import dateparser
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv, json
import psycopg2
from airflow import settings
from psycopg2.extras import execute_values
from psycopg2.extensions import register_adapter, AsIs
from airflow import settings
from sqlalchemy import create_engine
from airflow import DAG
from selenium.common.exceptions import NoSuchElementException
from airflow.operators.python_operator import PythonOperator
from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.bash_operator import BashOperator
from airflow.utils.log.logging_mixin import LoggingMixin
from typing import Callable
from airflow.utils.task_group import TaskGroup
import logging
from logging import handlers
from airflow import models
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.hooks.base_hook import BaseHook
from airflow.models import Variable
import time
from datetime import datetime, timedelta
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium import webdriver
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.chrome.options import Options as ChromeOptions
import pandas as pd
import numpy as np
import os

# Connections settings
# Загружаем данные подключений из JSON файла
with open('/opt/airflow/dags/config_connections.json', 'r') as conn_file:
    connections_config = json.load(conn_file)

# Получаем данные конфигурации подключения и создаем конфиг для клиента
conn_config = connections_config['psql_connect']

config = {
    'database': conn_config['database'],
    'user': conn_config['user'],
    'password': conn_config['password'],
    'host': conn_config['host'],
    'port': conn_config['port'],
}

conn = psycopg2.connect(**config)

# Variables settings
# Загружаем переменные из JSON файла
with open('/opt/airflow/dags/config_variables.json', 'r') as config_file:
    my_variables = json.load(config_file)

# Проверяем, существует ли переменная с данным ключом
if not Variable.get("shares_variable", default_var=None):
    # Если переменная не существует, устанавливаем ее
    Variable.set("shares_variable", my_variables, serialize_json=True)

dag_variables = Variable.get("shares_variable", deserialize_json=True)

url_sber = dag_variables.get('base_sber')
url_yand = dag_variables.get('base_yand')
url_vk = dag_variables.get('base_vk')
url_tin = dag_variables.get('base_tin')

raw_tables = ['raw_vk', 'raw_sber', 'raw_tinkoff', 'raw_yandex', 'del_vacancy_core']

options = ChromeOptions()

profs = dag_variables.get('professions')

logging_level = os.environ.get('LOGGING_LEVEL', 'DEBUG').upper()
logging.basicConfig(level=logging_level)
log = logging.getLogger(__name__)
log_handler = handlers.RotatingFileHandler('/opt/airflow/logs/airflow.log',
                                           maxBytes=5000,
                                           backupCount=5)

log_handler.setLevel(logging.DEBUG)
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_handler.setFormatter(log_formatter)
log.addHandler(log_handler)

# Параметры по умолчанию
default_args = {
    "owner": "admin_1T",
    # 'start_date': days_ago(1),
    'retry_delay': timedelta(minutes=5),
}

# Создаем DAG ручного запуска (инициализирующий режим).
initial_dag = DAG(dag_id='initial_dag',
                tags=['admin_1T'],
                start_date=datetime(2023, 10, 29),
                schedule_interval=None,
                default_args=default_args
                )

class DatabaseManager:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()
        self.raw_tables = raw_tables
        self.log = LoggingMixin().log

    def create_raw_tables(self):
        table_name = 'raw_vk'
        try:
            drop_table_query = f"DROP TABLE IF EXISTS {table_name};"
            self.cur.execute(drop_table_query)
            self.log.info(f'Удалена таблица {table_name}')

            create_table_query = f"""                
            CREATE TABLE {table_name}(
               vacancy_id VARCHAR(2083) NOT NULL,
               vacancy_name VARCHAR(255),
               towns VARCHAR(255),
               level VARCHAR(255),
               company VARCHAR(255),
               salary_from BIGINT,
               salary_to BIGINT,
               exp_from SMALLINT,
               exp_to SMALLINT,
               description TEXT,
               job_type VARCHAR(255),
               job_format VARCHAR(255),
               languages VARCHAR(255),
               skills VARCHAR(511),
               source_vac VARCHAR(255),
               date_created DATE,
               date_of_download DATE NOT NULL, 
               status VARCHAR(32),
               date_closed DATE,
               version_vac INTEGER NOT NULL,
               actual SMALLINT,
               PRIMARY KEY(vacancy_id)
            );
            """
            self.cur.execute(create_table_query)
            self.conn.commit()
            self.log.info(f'Таблица {table_name} создана в базе данных.')
        except Exception as e:
            self.log.error(f'Ошибка при создании таблицы {table_name}: {e}')
            self.conn.rollback()



class BaseJobParser:
    def __init__(self, url, profs, log, conn):
        self.browser = webdriver.Remote(command_executor='http://selenium-router:4444/wd/hub', options=options)
        self.url = url
        self.browser.get(self.url)
        self.browser.maximize_window()
        self.browser.delete_all_cookies()
        time.sleep(2)
        self.profs = profs
        self.log = log
        self.conn = conn

    def scroll_down_page(self, page_height=0):
        """
        Метод прокрутки страницы
        """
        self.browser.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        new_page_height = self.browser.execute_script('return document.body.scrollHeight')
        if new_page_height > page_height:
            self.scroll_down_page(new_page_height)

    def stop(self):
        """
        Метод для выхода из Selenium Webdriver
        """
        self.browser.quit()

    def find_vacancies(self):
        """
        Метод для парсинга вакансий, должен быть переопределен в наследниках
        """
        raise NotImplementedError("Вы должны определить метод find_vacancies")

    def save_df(self):
        """
        Метод для сохранения данных из pandas DataFrame
        """
        raise NotImplementedError("Вы должны определить метод save_df")


class VKJobParser(BaseJobParser):
    """
    Парсер вакансий с сайта VK, наследованный от BaseJobParser
    """
    def find_vacancies(self):
        """
        Метод для нахождения вакансий с VK
        """
        self.cur = self.conn.cursor()
        self.df = pd.DataFrame(columns=['vacancy_id', 'vacancy_name', 'towns', 'company', 'source_vac', 'date_created',
                                        'date_of_download', 'status', 'version_vac', 'actual', 'description'])
        self.log.info("Создан DataFrame для записи вакансий")
        self.browser.implicitly_wait(3)
        # Поиск и запись вакансий на поисковой странице
        for prof in self.profs:
            input_button = self.browser.find_element(By.XPATH,
                                                     '/html/body/div/div[1]/div[1]/div/form/div[1]/div[4]/div/div/div/div/input')
            input_button.send_keys(prof['fullName'])
            click_button = self.browser.find_element(By.XPATH,
                                                     '/html/body/div/div[1]/div[1]/div/form/div[1]/div[4]/div/button')
            click_button.click()
            time.sleep(5)

            # Прокрутка вниз до конца страницы
            self.scroll_down_page()

            try:
                vacs_bar = self.browser.find_element(By.XPATH, '/html/body/div/div[1]/div[2]/div/div')
                vacs = vacs_bar.find_elements(By.CLASS_NAME, 'result-item')
                vacs = [div for div in vacs if 'result-item' in str(div.get_attribute('class'))]
                self.log.info(f"Парсим вакансии по запросу: {prof['fullName']}")
                self.log.info(f"Количество: " + str(len(vacs)) + "\n")

                for vac in vacs:
                    vac_info = {}
                    vac_info['vacancy_id'] = str(vac.get_attribute('href'))
                    vac_info['vacancy_name'] = str(vac.find_element(By.CLASS_NAME, 'title-block').text)
                    vac_info['towns'] = str(vac.find_element(By.CLASS_NAME, 'result-item-place').text)
                    vac_info['company'] = str(vac.find_element(By.CLASS_NAME, 'result-item-unit').text)
                    self.df.loc[len(self.df)] = vac_info

            except Exception as e:
                self.log.error(f"Произошла ошибка: {e}")
                input_button.clear()

        self.df = self.df.drop_duplicates()
        self.log.info("Общее количество найденных вакансий после удаления дубликатов: " + str(len(self.df)) + "\n")
        self.df['date_created'] = datetime.now().date()
        self.df['date_of_download'] = datetime.now().date()
        self.df['source_vac'] = url_vk
        self.df['status'] = 'existing'
        self.df['version_vac'] = 1
        self.df['actual'] = 1

    def find_vacancies_description(self):
        """
        Метод для парсинга описаний вакансий для VKJobParser.
        """
        self.log.info('Старт парсинга описаний вакансий')
        if not self.df.empty:
            for descr in self.df.index:
                try:
                    vacancy_id = self.df.loc[descr, 'vacancy_id']
                    self.browser.get(vacancy_id)
                    self.browser.delete_all_cookies()
                    time.sleep(3)
                    desc = self.browser.find_element(By.CLASS_NAME, 'section').text
                    desc = desc.replace(';', '')
                    self.df.loc[descr, 'description'] = str(desc)

                except Exception as e:
                    self.log.error(f"Произошла ошибка: {e}, ссылка {self.df.loc[descr, 'vacancy_id']}")
                    pass
        else:
            self.log.info(f"Нет вакансий для парсинга")

    def save_df(self):
        """
        Метод для сохранения данных в базу данных vk
        """
        self.cur = self.conn.cursor()

        def addapt_numpy_float64(numpy_float64):
            return AsIs(numpy_float64)

        def addapt_numpy_int64(numpy_int64):
            return AsIs(numpy_int64)

        register_adapter(np.float64, addapt_numpy_float64)
        register_adapter(np.int64, addapt_numpy_int64)

        try:
            if not self.df.empty:
                self.log.info(f"Проверка типов данных в DataFrame: \n {self.df.dtypes}")
                table_name = raw_tables[0]
                # данные, которые вставляются в таблицу PosqtgreSQL
                data = [tuple(x) for x in self.df.to_records(index=False)]
                # формируем строку запроса с плейсхолдерами для значений
                query = f"INSERT INTO {table_name} (vacancy_id, vacancy_name, towns, company, source_vac, " \
                        f"date_created, date_of_download, status, version_vac, actual, description) VALUES %s"
                # исполняем запрос с использованием execute_values
                self.log.info(f"Запрос вставки данных: {query}")
                execute_values(self.cur, query, data)

                self.conn.commit()
                # логируем количество обработанных вакансий
                self.log.info("Общее количество загруженных в БД вакансий: " + str(len(self.df)) + "\n")

        except Exception as e:
            self.log.error(f"Ошибка при сохранении данных в функции 'save_df': {e}")
            raise

        finally:
            # закрываем курсор и соединение с базой данных
            self.cur.close()
            self.conn.close()


db_manager = DatabaseManager(conn=conn)

def init_run_vk_parser(**context):
    """
    Основной вид задачи для запуска парсера для вакансий VK
    """
    log = context['ti'].log
    log.info('Запуск парсера ВК')
    try:
        parser = VKJobParser(url_vk, profs, log, conn)
        parser.find_vacancies()
        parser.find_vacancies_description()
        parser.save_df()
        parser.stop()
        log.info('Парсер ВК успешно провел работу')
    except Exception as e:
        log.error(f'Ошибка во время работы парсера ВК: {e}')


hello_bash_task = BashOperator(
    task_id='hello_task',
    bash_command='echo "Желаю удачного парсинга! Да прибудет с нами безотказный интернет!"')

# Определение задачи
create_raw_tables = PythonOperator(
    task_id='create_raw_tables',
    python_callable=db_manager.create_raw_tables,
    provide_context=True,
    dag=initial_dag
)

parse_vkjobs = PythonOperator(
    task_id='parse_vkjobs',
    python_callable=init_run_vk_parser,
    provide_context=True,
    dag=initial_dag
)

end_task = DummyOperator(
    task_id="end_task"
)

hello_bash_task >> create_raw_tables >> parse_vkjobs >> end_task
