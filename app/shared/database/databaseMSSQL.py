import os
import pyodbc
from dotenv import load_dotenv

load_dotenv()

ERROR = pyodbc.Error

def get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise Exception(f"Missing environment variable: {name}")
    return value

def get_connection() -> pyodbc.Connection:
    """Crea y retorna la conexión a SQL Server"""
    connection_string = (
        f"DRIVER={get_env('MSSQL_DRIVER')};"
        f"SERVER={get_env('MSSQL_SERVER')},{get_env('MSSQL_PORT')};"
        f"DATABASE={get_env('MSSQL_DATABASE')};"
        f"UID={get_env('MSSQL_USER')};"
        f"PWD={get_env('MSSQL_PASSWORD')};"
        f"TrustServerCertificate=yes;"
        f"Encrypt=yes;"
    )
    return pyodbc.connect(connection_string)