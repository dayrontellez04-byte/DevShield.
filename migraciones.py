"""
DevShield - Migraciones mínimas de la base SQLite (sin Flask ni SQLAlchemy).

`db.create_all()` crea tablas nuevas pero NO agrega columnas a tablas que ya existen; esto
permite actualizar un devshield.db de una versión anterior sin perder datos.
"""
import os
import sqlite3

# tabla -> [(columna, definición SQL)]
COLUMNAS_NUEVAS = {
    'roles_mercado': [
        ('experiencia', "VARCHAR(10) NOT NULL DEFAULT 'JUNIOR'"),
        ('moneda', "VARCHAR(3) NOT NULL DEFAULT 'COP'"),
    ],
}


def asegurar_columnas(ruta_db: str) -> list:
    """Agrega las columnas que falten. Devuelve la lista de 'tabla.columna' agregadas."""
    if not os.path.exists(ruta_db):
        return []
    agregadas = []
    con = sqlite3.connect(ruta_db)
    try:
        for tabla, columnas in COLUMNAS_NUEVAS.items():
            existe = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tabla,)).fetchone()
            if not existe:
                continue
            actuales = {fila[1] for fila in con.execute(f'PRAGMA table_info({tabla})')}
            for nombre, definicion in columnas:
                if nombre not in actuales:
                    con.execute(f'ALTER TABLE {tabla} ADD COLUMN {nombre} {definicion}')
                    agregadas.append(f'{tabla}.{nombre}')
        con.commit()
    finally:
        con.close()
    return agregadas
