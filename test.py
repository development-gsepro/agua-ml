from app.consumoAgua import cargar_setpoints
import pandas as pd

setpoint = cargar_setpoints()

registros = []

for clave, valores in setpoint.items():

    id_vbox, medidor = clave

    registros.append({
        "idVBox": id_vbox,
        "Medidor": medidor,
        "weekdaysValue": valores.weekdaysValue,
        "weekendValue": valores.weekendValue,
    })

df = pd.DataFrame(registros)

df.to_excel("setpoints.xlsx", index=False)

print()
print("==========================================")
print("EXCEL GENERADO")
print("==========================================")
print(f"Registros: {len(df)}")
print("Archivo: setpoints.xlsx")