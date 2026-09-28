"""Runner sin pytest: ejecuta las pruebas de equivalencia con Python estándar."""
import inspect
import test_equivalencia as t

fallos = []
pruebas = [
    obj for name, obj in vars(t).items()
    if name.startswith("test_") and inspect.isfunction(obj)
]

for prueba in pruebas:
    try:
        prueba()
        print(f"OK  {prueba.__name__}")
    except Exception as exc:
        fallos.append((prueba.__name__, exc))
        print(f"FAIL {prueba.__name__}: {exc}")

print(f"\nResultado: {len(pruebas)-len(fallos)}/{len(pruebas)} pruebas OK")
if fallos:
    raise SystemExit(1)
