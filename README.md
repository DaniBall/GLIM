# Migrador de grupos GitLab

Herramienta en Python para migrar los repositorios de un grupo o subgrupo entre dos instancias GitLab, conservando la jerarquía relativa, el historial Git y la configuración operativa de CI/CD.

El script trabaja en **modo simulación por defecto**. Solo modifica el destino al añadir `--execute`.


## Índice

- [Alcance](#alcance)
- [Requisitos y permisos](#requisitos-y-permisos)
- [Instalación y comprobación](#instalación-y-comprobación)
- [Inicio seguro de una sesión](#inicio-seguro-de-una-sesión)
- [Grupos y subgrupos](#grupos-y-subgrupos)
- [Dry-run](#dry-run)
- [Ejecución real](#ejecución-real)
- [Proyectos existentes](#proyectos-existentes)
- [Excluir variables CI/CD](#excluir-variables-cicd)
- [Informes, checkpoint y reanudación](#informes-checkpoint-y-reanudación)
- [Opciones habituales](#opciones-habituales)
- [CA corporativa y TLS](#ca-corporativa-y-tls)
- [Desarrollo y validación](#desarrollo-y-validación)
- [Piloto y controles](#piloto-y-controles)
- [Solución de problemas](#solución-de-problemas)
- [Seguridad operativa](#seguridad-operativa)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Política de secretos y artefactos](#política-de-secretos-y-artefactos)

## Alcance

### Qué migra

- Repositorios mediante `git clone --mirror` y `git push --mirror`.
- Commits, merge commits, ramas, etiquetas y demás referencias incluidas en el mirror.
- Subgrupos descendientes y su jerarquía relativa.
- Git LFS y wikis, cuando están habilitados.
- Variables CI/CD accesibles de grupos, subgrupos y proyectos.
- Ajustes CI/CD compatibles, ramas y etiquetas protegidas.
- Pipeline schedules, creadas inicialmente inactivas.
- `.gitlab-ci.yml` y plantillas versionadas en Git.
- Deploy keys cuyo material público sea recuperable.
- Entornos protegidos compatibles.
- Ámbito y allowlists de `CI_JOB_TOKEN` dentro del alcance migrado.
- Estado archivado de los proyectos.

### Qué no migra

- Historial, jobs, trazas, logs y artefactos de pipelines.
- Runners y sus tokens; pipeline triggers y sus tokens.
- Container Registry, Package Registry, imágenes o paquetes.
- Issues, merge requests, comentarios, aprobaciones y otros metadatos colaborativos.
- Webhooks, Secure Files, deploy tokens o secretos que la API no pueda devolver.
- Asociación visual de autores cuando sus correos no coinciden con cuentas verificadas del destino.

Los merge commits sí se conservan como objetos Git; las merge requests no se reconstruyen como entidades. Las identidades o reglas que no puedan mapearse de forma inequívoca no se fuerzan: el resultado queda `partial` y el informe indica la intervención necesaria.


[Volver al índice](#índice)

## Requisitos y permisos

- Python 3.10 o posterior.
- Git en `PATH`.
- Git LFS en `PATH` si se migran objetos LFS.
- Acceso HTTPS a ambas instancias.
- Espacio temporal suficiente para el repositorio más grande.
- CA corporativa reconocida por Python y Git si se usa una PKI privada.
- Runners y tags necesarios ya disponibles en destino antes de activar pipelines.

Variables obligatorias:

- `GITLAB_SOURCE_TOKEN`: origen, con `read_api` y `read_repository`; usa `api` solo si GitLab lo exige.
- `GITLAB_TARGET_TOKEN`: destino, con `api`.

El propietario del token debe tener acceso suficiente en origen y, preferiblemente, rol Owner sobre el grupo raíz de destino. El scope del token no concede permisos que su propietario no tenga. Algunas variables protegidas u ocultas pueden no ser recuperables; el informe las señalará.

No escribas tokens en el script, comandos, perfiles, `.env`, historiales ni archivos versionados.


[Volver al índice](#índice)

## Instalación y comprobación

Esta guía instala GLIM directamente desde el repositorio. La herramienta no tiene
dependencias externas de Python en tiempo de ejecución, pero sí necesita Git y,
si los proyectos contienen objetos LFS, Git LFS. La instalación recomendada usa
un entorno virtual para no alterar el Python global del equipo.

Al terminar esta sección tendrás disponible el comando `glim`. Todavía no se
realizará ninguna conexión a GitLab: `glim --help` no necesita tokens.

### Paso 1. Instalar los requisitos del sistema

Se necesita:

- Python 3.10 o posterior, con `venv` y `pip`.
- Git accesible desde `PATH`.
- Git LFS accesible desde `PATH` si se van a migrar objetos LFS.
- Conectividad HTTPS hacia los dos GitLab.

#### Windows

1. Instala una versión compatible de Python desde
   [python.org](https://www.python.org/downloads/windows/). Asegúrate de que el
   comando `python` quede disponible en `PATH`.
2. Instala Git desde [git-scm.com](https://git-scm.com/downloads/win). Si usas
   `winget`, el instalador oficial documenta este comando:

   ```powershell
   winget install --id Git.Git -e --source winget
   ```

3. Cierra y vuelve a abrir la terminal para que reconozca los comandos nuevos.
4. Comprueba las versiones desde PowerShell o CMD:

   ```text
   python --version
   git --version
   git lfs version
   ```

Si `git lfs version` no se reconoce, instala Git LFS desde
[git-lfs.com](https://git-lfs.com/), abre una terminal nueva y repite la
comprobación.

Si `python` abre Microsoft Store o no se encuentra, revisa los alias de
ejecución de aplicaciones de Windows o usa el lanzador `py -3` en los comandos
de esta guía.

#### Ubuntu o Debian

Instala todos los requisitos desde los repositorios del sistema:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git git-lfs
python3 --version
git --version
git lfs version
```

En otra distribución Linux, instala los paquetes equivalentes con su gestor de
paquetes y usa `python3` en lugar de `python` cuando corresponda.

### Paso 2. Descargar el código

Elige una carpeta de trabajo y clona el repositorio. Si ya dispones de una copia
local, entra directamente en su directorio y continúa en el paso 3.

PowerShell o CMD:

```text
git clone https://github.com/DaniBall/GLIM.git
cd GLIM
```

Linux con Bash:

```bash
git clone https://github.com/DaniBall/GLIM.git
cd GLIM
```

Antes de seguir, `glim.py`, `pyproject.toml` y `README.md` deben estar en el
directorio actual.

### Paso 3. Crear y activar el entorno virtual

El entorno se crea una sola vez. Hay que activarlo de nuevo cada vez que se abra
una terminal para utilizar el comando corto `glim`.

#### PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip --version
```

Si PowerShell bloquea `Activate.ps1`, permite scripts únicamente en el proceso
actual y repite la activación:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

#### CMD

```bat
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip --version
```

#### Linux con Bash

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip --version
```

Cuando el entorno está activo, el prompt suele mostrar `(.venv)` y `python`
apunta al intérprete aislado del proyecto.

### Paso 4. Instalar GLIM

Con el entorno virtual activo, actualiza las herramientas de empaquetado e
instala el proyecto en modo editable:

```text
python -m pip install --upgrade pip setuptools
python -m pip install --editable .
```

El modo editable registra el comando `glim`, pero mantiene como código fuente el
`glim.py` de esta carpeta. Esto permite actualizar la herramienta desde Git sin
crear copias manuales. La descarga de `setuptools` puede requerir acceso al
índice de paquetes configurado por la organización.

### Paso 5. Inicializar Git LFS

Ejecuta una vez por cuenta de usuario:

```text
git lfs install
```

Después valida que el ejecutable está disponible:

```text
git lfs version
```

Si la migración no debe incluir LFS, se puede omitir su instalación, pero todos
los comandos de dry-run y ejecución deberán llevar `--no-with-lfs`. Con LFS
habilitado, el dry-run solo avisa si falta `git-lfs`; `--execute` aborta antes de
modificar el destino.

### Paso 6. Comprobar la instalación

Con el entorno activo:

```text
glim --help
```

También puedes comprobar que el comando apunta a este entorno:

PowerShell:

```powershell
Get-Command glim
python -m pip show glim
```

CMD:

```bat
where glim
python -m pip show glim
```

Linux con Bash:

```bash
command -v glim
python -m pip show glim
```

La salida debe mostrar la ayuda de la CLI y la versión `glim` instalada desde la
ruta del repositorio. Si `glim` no se encuentra, confirma que el entorno está
activo o usa la ruta directa indicada a continuación.

### Usar GLIM sin activar el entorno

PowerShell o CMD:

```text
.\.venv\Scripts\glim.exe --help
```

Linux con Bash:

```bash
./.venv/bin/glim --help
```

También es posible ejecutar el archivo sin instalar el comando:

- PowerShell: `python .\glim.py --help`
- CMD: `python glim.py --help`
- Linux: `python3 ./glim.py --help`

### Actualizar o desinstalar

Para actualizar una copia instalada en modo editable, entra en el repositorio,
comprueba antes que no haya cambios locales y actualiza el código:

```text
git status --short
git pull --ff-only
python -m pip install --editable .
```

Para retirar el comando del entorno virtual:

```text
python -m pip uninstall glim
```

Para salir del entorno sin desinstalarlo:

```text
deactivate
```

La instalación ya está completa. El siguiente paso es cargar los tokens de forma
temporal como se explica en [Inicio seguro de una sesión](#inicio-seguro-de-una-sesión)
y ejecutar primero un [dry-run](#dry-run).


[Volver al índice](#índice)

## Inicio seguro de una sesión

Carga los tokens al empezar la sesión y elimínalos al terminar. No los guardes como variables permanentes.

### PowerShell: carga temporal

```powershell
$sourceSecure = Read-Host "Token de GitLab origen" -AsSecureString
$targetSecure = Read-Host "Token de GitLab destino" -AsSecureString
$env:GITLAB_SOURCE_TOKEN = [System.Net.NetworkCredential]::new('', $sourceSecure).Password
$env:GITLAB_TARGET_TOKEN = [System.Net.NetworkCredential]::new('', $targetSecure).Password
Remove-Variable sourceSecure, targetSecure
```

Limpieza:

```powershell
Remove-Item Env:GITLAB_SOURCE_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:GITLAB_TARGET_TOKEN -ErrorAction SilentlyContinue
```

### PowerShell: SecretManagement

Instalación inicial:

```powershell
Install-Module Microsoft.PowerShell.SecretManagement, Microsoft.PowerShell.SecretStore -Scope CurrentUser
Register-SecretVault -Name GitLabMigration -ModuleName Microsoft.PowerShell.SecretStore -DefaultVault
Set-SecretStoreConfiguration -Authentication Password -Interaction Prompt
```

Guardar una vez:

```powershell
Set-Secret -Name GitLabMigrationSourceToken -Vault GitLabMigration -Secret (Read-Host "Token origen" -AsSecureString)
Set-Secret -Name GitLabMigrationTargetToken -Vault GitLabMigration -Secret (Read-Host "Token destino" -AsSecureString)
```

Cargar al iniciar cada sesión:

```powershell
$env:GITLAB_SOURCE_TOKEN = Get-Secret -Name GitLabMigrationSourceToken -Vault GitLabMigration -AsPlainText
$env:GITLAB_TARGET_TOKEN = Get-Secret -Name GitLabMigrationTargetToken -Vault GitLabMigration -AsPlainText
```

### CMD: carga temporal

CMD no tiene entrada secreta nativa. `set /P` evita incluir el valor en el comando, pero muestra los caracteres:

```bat
set "GITLAB_SOURCE_TOKEN="
set "GITLAB_TARGET_TOKEN="
set /P "GITLAB_SOURCE_TOKEN=Token del GitLab origen: "
set /P "GITLAB_TARGET_TOKEN=Token del GitLab destino: "
```

Limpieza:

```bat
set "GITLAB_SOURCE_TOKEN="
set "GITLAB_TARGET_TOKEN="
```

Para ocultar la entrada o cargar SecretManagement, abre PowerShell y ejecuta también allí la migración:

```bat
powershell -NoProfile
```

Las variables de un PowerShell hijo no vuelven al CMD padre. No se recomienda `cmdkey`: CMD puede guardar una credencial genérica, pero no recuperar de forma nativa y segura el secreto para Python.

### Linux con Bash: carga temporal

```bash
read -rsp 'Token del GitLab origen: ' GITLAB_SOURCE_TOKEN
printf '\n'
read -rsp 'Token del GitLab destino: ' GITLAB_TARGET_TOKEN
printf '\n'
export GITLAB_SOURCE_TOKEN GITLAB_TARGET_TOKEN
```

Limpieza:

```bash
unset GITLAB_SOURCE_TOKEN GITLAB_TARGET_TOKEN
```

Con Secret Service y `secret-tool`, guardar una vez:

```bash
read -rsp 'Token origen: ' TOKEN_TEMP
printf '%s' "$TOKEN_TEMP" | secret-tool store --label='GitLab migration source' service gitlab-migration account source
unset TOKEN_TEMP
read -rsp 'Token destino: ' TOKEN_TEMP
printf '%s' "$TOKEN_TEMP" | secret-tool store --label='GitLab migration target' service gitlab-migration account target
unset TOKEN_TEMP
```

Cargar al iniciar:

```bash
GITLAB_SOURCE_TOKEN="$(secret-tool lookup service gitlab-migration account source)"
GITLAB_TARGET_TOKEN="$(secret-tool lookup service gitlab-migration account target)"
export GITLAB_SOURCE_TOKEN GITLAB_TARGET_TOKEN
```

Alternativa con un almacén `pass` ya inicializado:

```bash
pass insert gitlab-migration/source
pass insert gitlab-migration/target
GITLAB_SOURCE_TOKEN="$(pass show gitlab-migration/source)"
GITLAB_TARGET_TOKEN="$(pass show gitlab-migration/target)"
export GITLAB_SOURCE_TOKEN GITLAB_TARGET_TOKEN
```

### Ubuntu con Bash: carga temporal y almacén

```bash
read -rsp 'Token del GitLab origen: ' GITLAB_SOURCE_TOKEN
printf '\n'
read -rsp 'Token del GitLab destino: ' GITLAB_TARGET_TOKEN
printf '\n'
export GITLAB_SOURCE_TOKEN GITLAB_TARGET_TOKEN
```

Instalar Secret Service:

```bash
sudo apt update
sudo apt install -y libsecret-tools
```

Guardar una vez:

```bash
read -rsp 'Token origen: ' TOKEN_TEMP
printf '%s' "$TOKEN_TEMP" | secret-tool store --label='GitLab migration source' service gitlab-migration account source
unset TOKEN_TEMP
read -rsp 'Token destino: ' TOKEN_TEMP
printf '%s' "$TOKEN_TEMP" | secret-tool store --label='GitLab migration target' service gitlab-migration account target
unset TOKEN_TEMP
```

Cargar al iniciar y limpiar al terminar:

```bash
GITLAB_SOURCE_TOKEN="$(secret-tool lookup service gitlab-migration account source)"
GITLAB_TARGET_TOKEN="$(secret-tool lookup service gitlab-migration account target)"
export GITLAB_SOURCE_TOKEN GITLAB_TARGET_TOKEN
# Después de la migración:
unset GITLAB_SOURCE_TOKEN GITLAB_TARGET_TOKEN
```

`secret-tool` requiere una sesión Secret Service desbloqueada. En servidores sin escritorio, usa un gestor corporativo o `pass` con GPG. No añadas tokens literales a `.bashrc`, `.profile` o scripts.

[Volver al índice](#índice)

## Grupos y subgrupos

`--source-group` y `--target-group` aceptan rutas completas, por ejemplo:

```text
Origen:  empresa/brasil/aplicaciones
Destino: migraciones/brasil/aplicaciones
```

El grupo raíz de destino debe existir. Los subgrupos descendientes sí los crea el migrador, conservando su ruta relativa. Por defecto se incluyen; añade `--no-include-subgroups` para migrar solo proyectos directos. Añade `--include-archived` para incluir archivados. No se migran grupos superiores, hermanos ni namespaces fuera de la raíz indicada.


[Volver al índice](#índice)

## Dry-run

El dry-run consulta ambas APIs y genera el plan sin modificar el destino. Requiere los dos tokens. Revisa el informe antes de ejecutar realmente.

### PowerShell

```powershell
glim `
  --source-url "https://gitlab-origen.example.com" `
  --target-url "https://gitlab.empresa.example" `
  --source-group "empresa/brasil/aplicaciones" `
  --target-group "migraciones/brasil/aplicaciones" `
  --checkpoint ".\checkpoints\brasil-aplicaciones.json" `
  --report ".\migration-dry-run.json" `
  --verbose
```

### CMD

```bat
glim ^
  --source-url "https://gitlab-origen.example.com" ^
  --target-url "https://gitlab.empresa.example" ^
  --source-group "empresa/brasil/aplicaciones" ^
  --target-group "migraciones/brasil/aplicaciones" ^
  --checkpoint "checkpoints\brasil-aplicaciones.json" ^
  --report "migration-dry-run.json" ^
  --verbose
```

### Linux con Bash

```bash
glim \
  --source-url "https://gitlab-origen.example.com" \
  --target-url "https://gitlab.empresa.example" \
  --source-group "empresa/brasil/aplicaciones" \
  --target-group "migraciones/brasil/aplicaciones" \
  --checkpoint "./checkpoints/brasil-aplicaciones.json" \
  --report "./migration-dry-run.json" \
  --verbose
```

### Ubuntu con Bash

```bash
glim \
  --source-url "https://gitlab-origen.example.com" \
  --target-url "https://gitlab.empresa.example" \
  --source-group "empresa/brasil/aplicaciones" \
  --target-group "migraciones/brasil/aplicaciones" \
  --checkpoint "./checkpoints/brasil-aplicaciones.json" \
  --report "./migration-dry-run.json" \
  --verbose
```


[Volver al índice](#índice)

## Ejecución real

Usa una ruta de checkpoint que no contenga evidencia de otra migración. Durante la carga, el script intenta mantener CI/CD deshabilitado. Las schedules se crean inactivas.

### PowerShell

```powershell
glim `
  --source-url "https://gitlab-origen.example.com" `
  --target-url "https://gitlab.empresa.example" `
  --source-group "empresa/brasil/aplicaciones" `
  --target-group "migraciones/brasil/aplicaciones" `
  --checkpoint ".\checkpoints\brasil-aplicaciones.json" `
  --report ".\migration-execution.json" `
  --execute
```

### CMD

```bat
glim ^
  --source-url "https://gitlab-origen.example.com" ^
  --target-url "https://gitlab.empresa.example" ^
  --source-group "empresa/brasil/aplicaciones" ^
  --target-group "migraciones/brasil/aplicaciones" ^
  --checkpoint "checkpoints\brasil-aplicaciones.json" ^
  --report "migration-execution.json" ^
  --execute
```

### Linux con Bash

```bash
glim \
  --source-url "https://gitlab-origen.example.com" \
  --target-url "https://gitlab.empresa.example" \
  --source-group "empresa/brasil/aplicaciones" \
  --target-group "migraciones/brasil/aplicaciones" \
  --checkpoint "./checkpoints/brasil-aplicaciones.json" \
  --report "./migration-execution.json" \
  --execute
```

### Ubuntu con Bash

```bash
glim \
  --source-url "https://gitlab-origen.example.com" \
  --target-url "https://gitlab.empresa.example" \
  --source-group "empresa/brasil/aplicaciones" \
  --target-group "migraciones/brasil/aplicaciones" \
  --checkpoint "./checkpoints/brasil-aplicaciones.json" \
  --report "./migration-execution.json" \
  --execute
```


[Volver al índice](#índice)

## Proyectos existentes

El valor predeterminado es `--existing skip`. `--existing update` puede reemplazar o eliminar referencias exclusivas del destino mediante push mirror. Úsalo solo cuando el proyecto destino pertenezca a esta migración, no tenga trabajo exclusivo ni actividad concurrente y exista una copia recuperable.

Añade `--existing update` al comando completo de dry-run y, después de revisar el informe, repítelo también con `--execute`:

- PowerShell: añade una línea ``--existing update ` `` antes de la última opción.
- CMD: añade una línea `--existing update ^` antes de la última opción.
- Linux/Bash: añade una línea `--existing update \` antes de la última opción.
- Ubuntu/Bash: añade una línea `--existing update \` antes de la última opción.

También puedes usar `--overwrite-ci-variables` si autorizas actualizar variables CI/CD ya existentes. Haz primero un dry-run con exactamente esa opción.


[Volver al índice](#índice)

## Excluir variables CI/CD

Añade `--no-ci-variables` cuando no deban consultarse ni copiarse variables
CI/CD. La opción omite las variables de grupos, proyectos y pipeline schedules.
Las schedules y sus inputs sí se migran y permanecen inactivos; únicamente se
excluyen sus variables.

`--no-ci-variables` no puede combinarse con `--overwrite-ci-variables`. Además,
forma parte de la identidad del checkpoint: si una migración se inició con esta
opción, hay que repetirla al usar `--resume`.

Ejemplo de dry-run:

```text
glim --source-url "https://gitlab-origen.example.com" --target-url "https://gitlab.empresa.example" --source-group "empresa/brasil/aplicaciones" --target-group "migraciones/brasil/aplicaciones" --no-ci-variables
```

[Volver al índice](#índice)

## Informes, checkpoint y reanudación

Cada migración que supera la validación inicial de argumentos y tokens escribe un informe JSON. Los errores de uso de la CLI, incluidos los argumentos incompletos o la ausencia de tokens, terminan antes de crear ese informe. Sin `--report`, se crea `migration-report-<fecha>.json`. Los estados son `planned`, `ok`, `skipped`, `partial` y `failed`. Aunque no deberían contener tokens ni valores CI/CD, los informes son información operativa interna.

El checkpoint predeterminado es `glim-checkpoint.json`. Registra identidad, proyectos y fases completadas, pero no secretos. Se recomienda indicar siempre una ruta dentro de `checkpoints/`, como en los ejemplos de esta guía, para mantener juntos los artefactos operativos. Si el checkpoint ya existe y no se indica `--resume`, el script aborta. Para una migración nueva usa otra ruta; para continuar, usa el mismo archivo, URLs, grupos y opciones ligadas.

Las variables heredadas desde grupos superiores se inventarían, pero no se copian fuera del alcance; generan `partial` para exigir revisión.

### PowerShell

```powershell
glim `
  --source-url "https://gitlab-origen.example.com" `
  --target-url "https://gitlab.empresa.example" `
  --source-group "empresa/brasil/aplicaciones" `
  --target-group "migraciones/brasil/aplicaciones" `
  --checkpoint ".\checkpoints\brasil-aplicaciones.json" `
  --report ".\migration-resume.json" `
  --resume `
  --execute
```

### CMD

```bat
glim ^
  --source-url "https://gitlab-origen.example.com" ^
  --target-url "https://gitlab.empresa.example" ^
  --source-group "empresa/brasil/aplicaciones" ^
  --target-group "migraciones/brasil/aplicaciones" ^
  --checkpoint "checkpoints\brasil-aplicaciones.json" ^
  --report "migration-resume.json" ^
  --resume ^
  --execute
```

### Linux con Bash

```bash
glim \
  --source-url "https://gitlab-origen.example.com" \
  --target-url "https://gitlab.empresa.example" \
  --source-group "empresa/brasil/aplicaciones" \
  --target-group "migraciones/brasil/aplicaciones" \
  --checkpoint "./checkpoints/brasil-aplicaciones.json" \
  --report "./migration-resume.json" \
  --resume \
  --execute
```

### Ubuntu con Bash

```bash
glim \
  --source-url "https://gitlab-origen.example.com" \
  --target-url "https://gitlab.empresa.example" \
  --source-group "empresa/brasil/aplicaciones" \
  --target-group "migraciones/brasil/aplicaciones" \
  --checkpoint "./checkpoints/brasil-aplicaciones.json" \
  --report "./migration-resume.json" \
  --resume \
  --execute
```

Si el checkpoint registra `existing_update`, repite `--existing update`. `--resume` no adopta proyectos ajenos. No edites el JSON sin auditar antes GitLab. Guarda checkpoint e informes juntos hasta cerrar la migración.


[Volver al índice](#índice)

## Opciones habituales

```text
--no-include-subgroups       No procesa subgrupos descendientes.
--include-archived           Incluye proyectos archivados.
--no-with-lfs                No migra Git LFS.
--no-with-wikis              No migra wikis.
--existing {skip,update}     Tratamiento de proyectos existentes.
--no-ci-variables            No consulta ni migra variables CI/CD.
--overwrite-ci-variables     Actualiza variables CI/CD existentes.
--resume                     Continúa desde el checkpoint.
--checkpoint PATH            JSON durable de progreso.
--report PATH                Informe JSON.
--ca-bundle PATH             CA PEM común.
--source-ca-bundle PATH      CA PEM solo para origen.
--target-ca-bundle PATH      CA PEM solo para destino.
--allow-http                 HTTP solo en laboratorios aislados.
--timeout 60                 Timeout HTTP de API.
--git-timeout 1800           Timeout de cada operación Git/LFS.
--min-free-space-mb 1024     Espacio temporal mínimo.
--retries 3                  Reintentos temporales.
--verbose                    Registro ampliado sin secretos.
```

Comprueba siempre la versión local:

- PowerShell, CMD y Bash: `glim --help`
- Sin instalar el comando: usa una de las alternativas descritas en
  [Usar GLIM sin activar el entorno](#usar-glim-sin-activar-el-entorno).

[Volver al índice](#índice)

## CA corporativa y TLS

Instalar la CA en los almacenes usados por el sistema, Python y Git es la opción recomendada. Los bundles del script son CA adicionales. `--ca-bundle` no puede combinarse con `--source-ca-bundle` o `--target-ca-bundle`: elige CA común o separadas. Nunca incluyas claves privadas.

### PowerShell

CA común:

```powershell
glim `
  --source-url "https://gitlab-origen.example.com" `
  --target-url "https://gitlab.empresa.example" `
  --source-group "empresa/brasil/aplicaciones" `
  --target-group "migraciones/brasil/aplicaciones" `
  --ca-bundle "C:\certificados\ca-corporativa.pem"
```

CA separadas:

```powershell
glim `
  --source-url "https://gitlab-origen.example.com" `
  --target-url "https://gitlab.empresa.example" `
  --source-group "empresa/brasil/aplicaciones" `
  --target-group "migraciones/brasil/aplicaciones" `
  --source-ca-bundle "C:\certificados\ca-origen.pem" `
  --target-ca-bundle "C:\certificados\ca-destino.pem"
```

Validación:

```powershell
git ls-remote "https://gitlab.empresa.example/grupo/proyecto.git"
python -c "import urllib.request; print(urllib.request.urlopen('https://gitlab.empresa.example').status)"
```

### CMD

CA común:

```bat
glim ^
  --source-url "https://gitlab-origen.example.com" ^
  --target-url "https://gitlab.empresa.example" ^
  --source-group "empresa/brasil/aplicaciones" ^
  --target-group "migraciones/brasil/aplicaciones" ^
  --ca-bundle "C:\certificados\ca-corporativa.pem"
```

CA separadas:

```bat
glim ^
  --source-url "https://gitlab-origen.example.com" ^
  --target-url "https://gitlab.empresa.example" ^
  --source-group "empresa/brasil/aplicaciones" ^
  --target-group "migraciones/brasil/aplicaciones" ^
  --source-ca-bundle "C:\certificados\ca-origen.pem" ^
  --target-ca-bundle "C:\certificados\ca-destino.pem"
```

Validación:

```bat
git ls-remote "https://gitlab.empresa.example/grupo/proyecto.git"
python -c "import urllib.request; print(urllib.request.urlopen('https://gitlab.empresa.example').status)"
```

### Linux con Bash

CA común:

```bash
glim \
  --source-url "https://gitlab-origen.example.com" \
  --target-url "https://gitlab.empresa.example" \
  --source-group "empresa/brasil/aplicaciones" \
  --target-group "migraciones/brasil/aplicaciones" \
  --ca-bundle "/ruta/ca-corporativa.pem"
```

CA separadas:

```bash
glim \
  --source-url "https://gitlab-origen.example.com" \
  --target-url "https://gitlab.empresa.example" \
  --source-group "empresa/brasil/aplicaciones" \
  --target-group "migraciones/brasil/aplicaciones" \
  --source-ca-bundle "/ruta/ca-origen.pem" \
  --target-ca-bundle "/ruta/ca-destino.pem"
```

Validación:

```bash
git ls-remote "https://gitlab.empresa.example/grupo/proyecto.git"
python3 -c "import urllib.request; print(urllib.request.urlopen('https://gitlab.empresa.example').status)"
```

### Ubuntu con Bash

CA común:

```bash
glim \
  --source-url "https://gitlab-origen.example.com" \
  --target-url "https://gitlab.empresa.example" \
  --source-group "empresa/brasil/aplicaciones" \
  --target-group "migraciones/brasil/aplicaciones" \
  --ca-bundle "/ruta/ca-corporativa.pem"
```

CA separadas:

```bash
glim \
  --source-url "https://gitlab-origen.example.com" \
  --target-url "https://gitlab.empresa.example" \
  --source-group "empresa/brasil/aplicaciones" \
  --target-group "migraciones/brasil/aplicaciones" \
  --source-ca-bundle "/ruta/ca-origen.pem" \
  --target-ca-bundle "/ruta/ca-destino.pem"
```

Validación:

```bash
git ls-remote "https://gitlab.empresa.example/grupo/proyecto.git"
python3 -c "import urllib.request; print(urllib.request.urlopen('https://gitlab.empresa.example').status)"
```

El script exige HTTPS. `--allow-http` es solo para laboratorios aislados; no lo uses para esquivar un error de certificado. `--git-timeout` evita bloqueos indefinidos y `--min-free-space-mb` aborta antes de modificar el destino si falta espacio.


[Volver al índice](#índice)

## Desarrollo y validación

Las pruebas usan respuestas simuladas y no deben conectarse a GitLab ni usar tokens reales.

### PowerShell

```powershell
python -m unittest discover -s tests -v
python -m py_compile .\glim.py
python .\glim.py --help
```

### CMD

```bat
python -m unittest discover -s tests -v
python -m py_compile glim.py
python glim.py --help
```

### Linux con Bash

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile ./glim.py
python3 ./glim.py --help
```

### Ubuntu con Bash

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile ./glim.py
python3 ./glim.py --help
```

Consulta [CONTRIBUTING.md](CONTRIBUTING.md). GitHub Actions ejecuta estas validaciones sin acceder a GitLab reales ni recibir tokens.


[Volver al índice](#índice)

## Piloto y controles

Antes de migrar un grupo completo, usa un namespace aislado con repositorios pequeños y grandes, ramas, tags, merge commits, LFS, wiki, variables de distintos tipos, schedules, protecciones, includes entre proyectos y jobs con los tags reales de runners. No actives schedules ni despliegues durante el piloto.

Checklist previo:

- [ ] Confirmar URLs, versiones y rutas completas de ambos grupos.
- [ ] Crear el grupo raíz de destino.
- [ ] Validar scopes, roles y visibilidad de proyectos.
- [ ] Comprobar compatibilidad Git, LFS, espacio, red y TLS.
- [ ] Inventariar runners, tags, includes y dependencias.
- [ ] Ejecutar el dry-run y revisar su informe.
- [ ] Acordar una ventana sin escrituras en origen.
- [ ] Preparar recuperación antes de usar `--existing update`.

Checklist posterior:

- [ ] Comparar ramas, tags y SHA; confirmar la rama predeterminada.
- [ ] Probar una muestra LFS y la wiki.
- [ ] Revisar variables, protecciones, schedules e includes.
- [ ] Confirmar runners y tags.
- [ ] Ejecutar CI Lint y una pipeline controlada sin despliegue.
- [ ] Confirmar que CI/CD se restauró.
- [ ] Revisar `partial` y `failed`.
- [ ] Mantener el origen hasta aprobar la migración.


[Volver al índice](#índice)

## Solución de problemas

### Verificar que los tokens están cargados

No imprimas su valor.

PowerShell:

```powershell
if ($env:GITLAB_SOURCE_TOKEN -and $env:GITLAB_TARGET_TOKEN) { "Tokens cargados" } else { "Faltan tokens" }
```

CMD:

```bat
if defined GITLAB_SOURCE_TOKEN (if defined GITLAB_TARGET_TOKEN (echo Tokens cargados) else (echo Faltan tokens)) else (echo Faltan tokens)
```

Linux con Bash:

```bash
if [[ -n "${GITLAB_SOURCE_TOKEN:-}" && -n "${GITLAB_TARGET_TOKEN:-}" ]]; then echo 'Tokens cargados'; else echo 'Faltan tokens'; fi
```

Ubuntu con Bash:

```bash
if [[ -n "${GITLAB_SOURCE_TOKEN:-}" && -n "${GITLAB_TARGET_TOKEN:-}" ]]; then echo 'Tokens cargados'; else echo 'Faltan tokens'; fi
```

### Errores habituales

- `HTTP 401`: token inválido, caducado, revocado o no cargado.
- `HTTP 403`: rol o scope insuficiente.
- `HTTP 404`: usa el namespace completo y verifica visibilidad; GitLab puede devolver 404 para recursos sin permiso.
- Error SSL: instala la CA o usa los bundles PEM; no desactives TLS.
- Falta `git-lfs`: instálalo y ejecuta `git lfs install`, o acepta `--no-with-lfs`.
- Proyecto existente: determina si está completo, parcial o es ajeno antes de `--existing update`.
- CI Lint falla: revisa `ci_config_path`, includes, `CI_JOB_TOKEN`, variables y compatibilidad de versiones.
- Pipeline pendiente: revisa runners, tags, protecciones y ámbitos; los runners no se migran.
- Schedule inactiva: es intencionado; valida ref, variables, zona horaria, inputs, propietario y runners antes de activarla.

Códigos de salida:

- `0`: sin elementos `partial` ni `failed`.
- `1`: fallo global o de preparación.
- `2`: existe al menos un `partial` o `failed`, o la CLI ha rechazado los
  argumentos o los tokens obligatorios antes de iniciar la migración.
- `130`: interrupción manual.


[Volver al índice](#índice)

## Seguridad operativa

- Usa HTTPS, CA válidas y tokens temporales de mínimo alcance.
- Elimina las variables de sesión y revoca los tokens al terminar.
- No captures ni publiques terminales, logs o informes sin revisarlos.
- No actives schedules ni pipelines antes de validar variables, runners e includes.
- Trata `--existing update` como potencialmente destructivo.
- Evita escrituras en origen durante la ventana.
- Restringe el acceso a informes y checkpoints.


[Volver al índice](#índice)

## Estructura del repositorio

```text
.
|-- glim.py   # Implementación y CLI
|-- tests/                     # Pruebas unitarias
|-- .github/workflows/         # GitHub Actions
|-- pyproject.toml             # Metadatos y comando instalable
|-- CONTRIBUTING.md            # Guía de contribución
|-- SECURITY.md                # Comunicación de vulnerabilidades
`-- README.md                  # Operación y desarrollo
```


[Volver al índice](#índice)

## Política de secretos y artefactos

Nunca subas tokens, `.env`, claves privadas, certificados internos, almacenes de credenciales, checkpoints o informes reales. `.gitignore` reduce el riesgo, pero no sustituye la revisión antes del commit. Si un secreto se versiona, revócalo y rótalo inmediatamente; borrarlo del último commit no basta. Para vulnerabilidades, sigue [SECURITY.md](SECURITY.md).

[Volver al índice](#índice)
