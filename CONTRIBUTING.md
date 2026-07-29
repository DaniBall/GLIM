# Contribuir

Gracias por ayudar a mejorar el migrador. Esta herramienta opera sobre repositorios y configuración sensible de GitLab, por lo que los cambios deben priorizar la seguridad, la trazabilidad y la compatibilidad entre versiones.

## Antes de empezar

- Abre una issue o conversación para cambios de alcance amplio.
- No incluyas datos reales de la empresa, URLs internas, nombres de proyectos, tokens, certificados, checkpoints o informes de migración.
- Para vulnerabilidades, sigue [SECURITY.md](SECURITY.md) y no abras una issue pública con detalles explotables.
- Mantén el comportamiento de simulación por defecto y evita nuevas operaciones destructivas implícitas.

## Entorno de desarrollo

Se necesita Python 3.10 o posterior. El proyecto usa exclusivamente la biblioteca estándar en tiempo de ejecución.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --editable .
```

## Validación local

Antes de proponer un cambio, ejecuta:

```powershell
python -m unittest discover -s tests -v
python -m py_compile .\glim.py
python .\glim.py --help
```

Si el cambio afecta a Git, LFS, APIs o CI/CD, añade pruebas unitarias con respuestas simuladas. Las pruebas no deben depender de instancias empresariales ni de credenciales reales.

## Criterios para los cambios

- Conserva compatibilidad con Python 3.10 o posterior.
- No añadas dependencias de ejecución sin justificar su necesidad y su impacto operativo.
- No registres tokens ni valores de variables CI/CD, tampoco en excepciones o fixtures.
- Mantén los errores recuperables por proyecto y los estados `ok`, `partial`, `failed`, `skipped` y `planned` coherentes.
- Documenta opciones nuevas, efectos laterales, requisitos de permisos y pasos de recuperación.
- Separa los cambios funcionales de los puramente documentales cuando sea posible.

## Pull requests

Describe el problema, el enfoque elegido, los riesgos y las validaciones realizadas. Incluye pruebas para cambios funcionales y actualiza el README cuando cambien la CLI, el alcance o el procedimiento operativo. No adjuntes capturas ni salidas sin revisar que estén completamente anonimizadas.
