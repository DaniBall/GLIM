# Migrador de grupos GitLab

Herramienta en Python para migrar los repositorios de un grupo o subgrupo desde una instancia de GitLab a otra, conservando su jerarquía relativa y dejando la configuración operativa de CI/CD preparada en el destino.

El script trabaja en **modo simulación por defecto**. Solo modifica el GitLab de destino cuando se añade `--execute`.

## Alcance

### Qué migra

- Repositorios Git mediante `clone --mirror` y `push --mirror`.
- Historial de commits y merge commits alcanzable desde las referencias migradas.
- Ramas, etiquetas y demás referencias Git incluidas en el mirror.
- Subgrupos descendientes y su jerarquía relativa, salvo que se desactiven.
- Git LFS, cuando `git-lfs` está instalado y la opción está habilitada.
- Wikis Git, cuando existen y la opción está habilitada.
- Variables CI/CD de grupos, subgrupos y proyectos accesibles mediante la API.
- Ajustes CI/CD compatibles entre las dos instancias.
- Ramas y etiquetas protegidas que el script puede reproducir de forma segura.
- Pipeline schedules, creadas inicialmente inactivas.
- El archivo `.gitlab-ci.yml` y las plantillas versionadas dentro de los repositorios.
- Deploy keys cuyo material público es recuperable, incluyendo permiso de escritura y caducidad cuando la API los expone.
- Entornos protegidos compatibles, con una política conservadora: no se aplican identidades sin mapear ni se relajan reglas distintas ya existentes.
- Configuración de ámbito y allowlists de `CI_JOB_TOKEN` para proyectos y grupos que estén dentro del ámbito migrado.
- Estado archivado de los proyectos incluidos en la migración.

### Qué no migra

- Historial de pipelines, jobs, trazas, logs ni artefactos.
- Runners ni sus tokens de registro o autenticación.
- Pipeline triggers ni sus tokens.
- Container Registry e imágenes.
- Package Registry y paquetes.
- Issues, merge requests, comentarios, aprobaciones y demás metadatos colaborativos.
- Webhooks, Secure Files, deploy tokens y otros secretos que la API de origen no pueda devolver.
- Asociación visual de autores con cuentas del nuevo GitLab cuando los correos de los commits no coinciden con correos verificados en destino.

Los commits y merge commits sí se conservan como objetos Git. Las merge requests de GitLab no se reconstruyen como entidades.

Las referencias a usuarios, grupos, deploy keys, entornos o entradas de allowlists externas que no puedan mapearse de forma inequívoca no se fuerzan. El elemento queda con estado `partial` y la intervención necesaria aparece en el informe.

## Requisitos

- Python 3.10 o posterior.
- Git disponible en `PATH`.
- Acceso de red HTTPS desde el equipo hacia ambos GitLab.
- Tokens personales o de grupo con los alcances y roles necesarios.
- Espacio temporal suficiente para clonar el repositorio más grande de la migración.
- `git-lfs` disponible en `PATH` si se migran objetos LFS.
- Certificado de la CA corporativa instalado y reconocido por Python y Git cuando el GitLab interno utiliza una PKI privada.
- Runners compatibles y con los tags requeridos ya disponibles en el GitLab destino antes de activar o ejecutar pipelines.

Comprobaciones rápidas:

```powershell
python --version
git --version
git lfs version
python .\migrate_gitlab_group.py --help
```

Si no se necesita LFS, se puede usar `--no-with-lfs`. Con LFS habilitado, la simulación avisa si falta `git-lfs`, pero una ejecución con `--execute` aborta antes de modificar el destino. Solo usa `--no-with-lfs` si has comprobado que los proyectos no utilizan LFS o aceptas expresamente no migrar sus objetos.

## Permisos y tokens

El script exige estas variables de entorno:

- `GITLAB_SOURCE_TOKEN`: token del GitLab de origen.
- `GITLAB_TARGET_TOKEN`: token del GitLab de destino.

Recomendación de permisos:

| Token | Alcances recomendados | Rol necesario |
|---|---|---|
| Origen | `read_api` y `read_repository`; usar `api` solo si la versión o configuración de GitLab lo exige | Acceso suficiente para leer todos los proyectos, variables CI/CD, schedules y protecciones del grupo |
| Destino | `api` | Preferiblemente Owner del grupo raíz de destino, o permisos equivalentes para crear subgrupos/proyectos y administrar CI/CD |

Los alcances del token y el rol del usuario son controles distintos: un token con `api` no concede acceso a proyectos que su propietario no puede administrar.

Para migrar variables de grupo desde un grupo raíz y crear subgrupos bajo él, normalmente se necesita ser Owner. En origen, las variables protegidas, ocultas o restringidas pueden no ser recuperables aunque el token sea válido; esos casos deben revisarse en el informe.

No escribas tokens en el script, en el README, en comandos que queden en el historial ni en archivos versionados.

## Variables de entorno en PowerShell

### Para la sesión actual

Esta es la forma recomendada para una ejecución puntual. Los valores desaparecen al cerrar la terminal:

```powershell
$env:GITLAB_SOURCE_TOKEN = Read-Host "Token de GitLab origen"
$env:GITLAB_TARGET_TOKEN = Read-Host "Token de GitLab destino"
```

`Read-Host` evita dejar el token escrito literalmente en el historial, aunque la entrada se muestra mientras se escribe. Si se prefiere ocultar la entrada:

```powershell
$sourceSecure = Read-Host "Token de GitLab origen" -AsSecureString
$targetSecure = Read-Host "Token de GitLab destino" -AsSecureString
$env:GITLAB_SOURCE_TOKEN = [System.Net.NetworkCredential]::new('', $sourceSecure).Password
$env:GITLAB_TARGET_TOKEN = [System.Net.NetworkCredential]::new('', $targetSecure).Password
Remove-Variable sourceSecure, targetSecure
```

Al terminar:

```powershell
Remove-Item Env:GITLAB_SOURCE_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:GITLAB_TARGET_TOKEN -ErrorAction SilentlyContinue
```

### Persistencia segura con PowerShell SecretManagement

No es recomendable guardar tokens directamente como variables de entorno permanentes de Windows: quedan almacenados como texto recuperable por el usuario. Una alternativa es conservarlos en un almacén de secretos y cargarlos en el entorno solo durante la sesión.

Instalación inicial de los módulos oficiales de PowerShell:

```powershell
Install-Module Microsoft.PowerShell.SecretManagement, Microsoft.PowerShell.SecretStore -Scope CurrentUser
Register-SecretVault -Name GitLabMigration -ModuleName Microsoft.PowerShell.SecretStore -DefaultVault
Set-SecretStoreConfiguration -Authentication Password -Interaction Prompt
```

Guardar los tokens una sola vez:

```powershell
Set-Secret -Name GitLabMigrationSourceToken -Vault GitLabMigration -Secret (Read-Host "Token origen" -AsSecureString)
Set-Secret -Name GitLabMigrationTargetToken -Vault GitLabMigration -Secret (Read-Host "Token destino" -AsSecureString)
```

Cargarlos al iniciar una sesión de migración:

```powershell
$env:GITLAB_SOURCE_TOKEN = Get-Secret -Name GitLabMigrationSourceToken -Vault GitLabMigration -AsPlainText
$env:GITLAB_TARGET_TOKEN = Get-Secret -Name GitLabMigrationTargetToken -Vault GitLabMigration -AsPlainText
```

El proceso de Python necesita finalmente los valores en variables de entorno, pero esta opción evita mantenerlos de forma permanente en el perfil de Windows o en un archivo. El acceso al almacén puede solicitar la contraseña configurada.

## Preparación de los grupos

Los argumentos `--source-group` y `--target-group` aceptan rutas completas de grupos o subgrupos:

```text
Origen:  empresa/brasil/aplicaciones
Destino: migraciones/brasil/aplicaciones
```

El grupo raíz indicado por `--target-group` debe existir antes de ejecutar el script. El migrador reproduce debajo de él los subgrupos descendientes encontrados en el origen.

Ejemplo:

```text
empresa/brasil/aplicaciones/backend/api
                          └───────────┘
                 jerarquía relativa

migraciones/brasil/aplicaciones/backend/api
```

Por defecto se incluyen los subgrupos. Para migrar únicamente los proyectos directamente contenidos en el grupo raíz:

```powershell
--no-include-subgroups
```

Los grupos superiores, hermanos y demás namespaces fuera de la raíz indicada no se migran.

## Simulación inicial

La simulación consulta las APIs y genera un plan, pero no crea ni modifica recursos en destino:

```powershell
python .\migrate_gitlab_group.py `
  --source-url "https://gitlab-origen.example.com" `
  --target-url "https://gitlab.empresa.example" `
  --source-group "empresa/brasil/aplicaciones" `
  --target-group "migraciones/brasil/aplicaciones" `
  --report ".\migration-dry-run.json" `
  --verbose
```

Aunque sea un dry-run, los dos tokens deben estar definidos porque el script valida grupos, permisos y recursos mediante las APIs.

Los proyectos archivados se excluyen por defecto. Para incluirlos:

```powershell
--include-archived
```

## Ejecución real

Después de revisar el informe de simulación:

```powershell
python .\migrate_gitlab_group.py `
  --source-url "https://gitlab-origen.example.com" `
  --target-url "https://gitlab.empresa.example" `
  --source-group "empresa/brasil/aplicaciones" `
  --target-group "migraciones/brasil/aplicaciones" `
  --report ".\migration-execution.json" `
  --execute
```

Durante la carga de cada proyecto, el script intenta mantener CI/CD deshabilitado para evitar despliegues accidentales. Las pipeline schedules se crean inactivas y no deben activarse hasta completar las verificaciones posteriores.

Opciones habituales:

```text
--no-include-subgroups       No procesa subgrupos descendientes.
--include-archived           Incluye proyectos archivados.
--no-with-lfs                No intenta migrar Git LFS.
--no-with-wikis              No intenta migrar wikis.
--overwrite-ci-variables     Actualiza variables CI/CD ya existentes.
--resume                     Continúa desde las fases guardadas en el checkpoint.
--checkpoint PATH            Define el JSON durable de progreso.
--ca-bundle PATH             Añade una CA PEM común para origen y destino.
--source-ca-bundle PATH      Añade una CA PEM únicamente para el GitLab origen.
--target-ca-bundle PATH      Añade una CA PEM únicamente para el GitLab destino.
--allow-http                 Permite HTTP sin TLS; solo para redes aisladas de prueba.
--timeout 120                Cambia el timeout HTTP de la API.
--git-timeout 3600           Cambia el timeout de cada operación Git o Git LFS.
--min-free-space-mb 4096     Exige espacio libre temporal antes de modificar destino.
--retries 5                  Cambia los reintentos ante errores temporales.
--verbose                    Amplía el registro sin mostrar secretos.
```

Consulta siempre `python .\migrate_gitlab_group.py --help` para confirmar las opciones de la versión local.

## Proyectos existentes

El comportamiento predeterminado es seguro:

```powershell
--existing skip
```

Si un proyecto ya existe en destino, se omite. Para actualizarlo explícitamente:

```powershell
--existing update
```

`--existing update` utiliza un push mirror y puede reemplazar o eliminar en destino referencias que no existan en origen. Debe usarse únicamente cuando se haya verificado que el proyecto destino pertenece a esta migración, no contiene trabajo exclusivo y existe una copia recuperable.

Si la ejecución inicial adoptó un proyecto preexistente con `--existing update`, su reanudación solo es válida con el mismo checkpoint y volviendo a indicar `--existing update`. `--resume` no adopta proyectos ajenos ni convierte automáticamente un proyecto existente en parte de la migración.

Antes de usarlo:

1. Revisa la ruta exacta del proyecto origen y destino.
2. Protege o exporta cualquier referencia exclusiva del destino.
3. Confirma que no hay actividad de usuarios en destino.
4. Ejecuta primero el dry-run con los mismos argumentos.
5. Conserva el informe anterior y el nuevo para auditoría.

## Informes, checkpoint y reanudación

Cada ejecución escribe un informe JSON. Si no se indica `--report`, se crea un archivo `migration-report-<fecha>.json` en el directorio actual.

El informe registra el modo, las rutas raíz, los elementos planificados o procesados, advertencias, errores y el resumen final. Los estados posibles son `planned`, `ok`, `skipped`, `partial` y `failed`. `partial` significa que la migración principal pudo avanzar, pero algún componente no pudo copiarse, verificarse o mapearse con seguridad. No debería contener valores de variables CI/CD ni tokens; aun así, trátalo como información operativa interna.

Las variables heredadas desde grupos superiores al grupo raíz se inventarían, pero no se copian fuera del ámbito indicado. Si existen, producen un elemento `partial` para exigir la revisión de colisiones y su réplica en un ancestro equivalente del destino.

El progreso durable se guarda por defecto en `migration-checkpoint.json` durante una ejecución real. Puede elegirse otra ubicación:

```powershell
--checkpoint ".\checkpoints\brasil-aplicaciones.json"
```

El checkpoint v2 registra la identidad de la migración, cada proyecto destino y las fases completadas. También liga las opciones que cambian el resultado durable: inclusión de subgrupos y archivados, LFS, wikis, tratamiento de proyectos existentes y sobrescritura de variables. No almacena tokens ni valores de variables CI/CD.

En una ejecución real, si la ruta de checkpoint ya existe y no se indica `--resume`, el script aborta para no sobrescribir evidencia previa. Para iniciar una migración nueva, usa una ruta de checkpoint nueva; para continuar la existente, usa `--resume` y exactamente las mismas opciones ligadas.

Para reanudar una ejecución interrumpida, usa exactamente las mismas URLs y grupos junto con el mismo archivo:

```powershell
python .\migrate_gitlab_group.py `
  --source-url "https://gitlab-origen.example.com" `
  --target-url "https://gitlab.empresa.example" `
  --source-group "empresa/brasil/aplicaciones" `
  --target-group "migraciones/brasil/aplicaciones" `
  --checkpoint ".\checkpoints\brasil-aplicaciones.json" `
  --report ".\migration-resume.json" `
  --resume `
  --execute
```

`--resume` exige que el checkpoint exista y que su identidad completa —URLs, grupos y opciones ligadas— coincida con la invocación. El migrador no adopta una ruta destino ajena al checkpoint. Si el checkpoint registra `migration_mode: existing_update`, también se debe repetir `--existing update`. No edites manualmente el JSON salvo que se haya auditado el estado real en GitLab.

Guarda checkpoint e informes juntos hasta cerrar la migración. El checkpoint permite continuar; el informe explica qué ocurrió en cada intento. Para una reejecución sin `--resume`, no asumas que la mera existencia de un proyecto significa que terminó correctamente.

## CA corporativa y TLS

La opción recomendada es instalar la CA corporativa en los almacenes de confianza utilizados por Windows, Python y Git. Los bundles indicados al script son CA adicionales y conservan las CA del sistema.

Si ambas instancias confían en la misma CA adicional:

```powershell
--ca-bundle "C:\certificados\ca-corporativa.pem"
```

Si cada instancia necesita una cadena distinta:

```powershell
--source-ca-bundle "C:\certificados\ca-origen.pem" `
--target-ca-bundle "C:\certificados\ca-destino.pem"
```

`--ca-bundle` no puede combinarse con `--source-ca-bundle` ni `--target-ca-bundle`: elige el modo común o el modo separado. Los bundles se aplican tanto a la API como a las operaciones Git de la instancia correspondiente. No incluyas claves privadas ni versiones el bundle si contiene material interno.

Valida ambos caminos antes de migrar:

```powershell
git ls-remote "https://gitlab.empresa.example/grupo/proyecto.git"
python -c "import urllib.request; print(urllib.request.urlopen('https://gitlab.empresa.example').status)"
```

El script exige HTTPS por defecto. `--allow-http` permite URLs `http://`, pero debe reservarse para laboratorios o redes aisladas sin credenciales reutilizables. No desactives TLS para esquivar un error de certificado en producción.

`--git-timeout` limita cada operación Git o Git LFS y evita bloqueos indefinidos; aumenta su valor para repositorios grandes o enlaces lentos. `--min-free-space-mb` aborta antes de efectuar cambios si el área temporal no tiene el mínimo requerido.

## Piloto recomendado

Antes de migrar un grupo completo, crea un namespace aislado en destino y prueba con un conjunto representativo que incluya:

- Un repositorio pequeño y otro grande.
- Ramas, etiquetas y merge commits.
- Git LFS.
- Wiki.
- Variables normales, protected, masked y con `environment_scope`.
- Pipeline schedules con variables.
- Ramas y etiquetas protegidas.
- `include:` entre proyectos o plantillas compartidas.
- Un `.gitlab-ci.yml` con los mismos tags de runners que producción.

No actives schedules ni ejecutes despliegues reales durante el piloto.

## Checklist previo

- [ ] Confirmar las URLs y versiones de ambos GitLab.
- [ ] Confirmar las rutas completas de los grupos raíz.
- [ ] Crear previamente el grupo raíz de destino.
- [ ] Validar roles y alcances de ambos tokens.
- [ ] Comprobar que todos los proyectos esperados son visibles para el token origen.
- [ ] Revisar formato de objetos Git y compatibilidad entre instancias.
- [ ] Instalar y probar Git LFS si aplica.
- [ ] Confirmar espacio temporal y conectividad.
- [ ] Instalar la CA corporativa y validar TLS.
- [ ] Inventariar runners, tags y capacidad disponible en destino.
- [ ] Inventariar includes CI externos, dependencias entre proyectos y URLs antiguas.
- [ ] Ejecutar dry-run y revisar completamente el informe.
- [ ] Acordar una ventana sin cambios o congelación de escrituras en origen.
- [ ] Preparar recuperación o copia de seguridad antes de actualizar proyectos existentes.

## Checklist posterior

- [ ] Comparar ramas, etiquetas y SHA de las referencias entre origen y destino.
- [ ] Confirmar la rama predeterminada.
- [ ] Descargar una muestra de objetos Git LFS.
- [ ] Abrir y clonar la wiki si aplica.
- [ ] Revisar variables CI/CD sin exponer sus valores.
- [ ] Revisar protecciones de ramas y etiquetas, especialmente reglas por usuario, grupo o deploy key.
- [ ] Revisar schedules y mantenerlas inactivas hasta la aprobación.
- [ ] Validar `include:` y rutas que todavía apunten al GitLab antiguo.
- [ ] Confirmar que los runners y sus tags resuelven todos los jobs.
- [ ] Ejecutar CI Lint y una pipeline controlada sin despliegue.
- [ ] Confirmar que CI/CD queda restaurado en cada proyecto.
- [ ] Revisar advertencias, parciales y fallos del informe.
- [ ] Mantener el origen disponible hasta aprobar la migración.

## Solución de problemas

### Faltan variables de entorno obligatorias

Define ambos tokens en la misma sesión de PowerShell desde la que se ejecuta Python:

```powershell
$env:GITLAB_SOURCE_TOKEN = Read-Host "Token origen"
$env:GITLAB_TARGET_TOKEN = Read-Host "Token destino"
```

### HTTP 401

El token es inválido, ha caducado, fue revocado o no se está cargando en el proceso. Vuelve a definirlo y comprueba su vigencia sin imprimirlo.

### HTTP 403

El usuario del token no tiene el rol necesario o el alcance del token es insuficiente. Revisa permisos sobre el grupo, variables, protecciones y creación de proyectos.

### HTTP 404 para un grupo existente

Usa la ruta completa del namespace, por ejemplo `empresa/brasil/aplicaciones`, y comprueba que el usuario del token pueda verlo. GitLab también puede responder 404 para recursos sin permiso.

### Error de certificado SSL

Instala la CA corporativa para Python y Git. No desactives la verificación TLS como solución permanente.

### `git-lfs` no está instalado

La simulación continúa con una advertencia, pero `--execute` aborta antes de modificar el destino. Instala `git-lfs` y ejecuta `git lfs install`, o usa expresamente `--no-with-lfs` solo si aceptas excluir esos objetos.

### El proyecto ya existe

El modo predeterminado lo omite. Revisa el informe y determina si es un proyecto completo, una migración parcial o un proyecto ajeno. No uses `--existing update` sin confirmar la procedencia y el impacto del mirror.

### CI Lint falla

Comprueba `ci_config_path`, includes a otros proyectos, permisos con `CI_JOB_TOKEN`, variables requeridas, componentes remotos y compatibilidad de sintaxis entre versiones de GitLab. Repite la validación cuando todas las dependencias estén migradas.

### La pipeline queda pendiente

Revisa que existan runners activos en destino y que sus tags, protecciones y ámbitos coincidan con los jobs. Los runners no se migran con este script.

### Una schedule no se ejecuta

Las schedules se crean inactivas intencionadamente. Revisa primero variables, ref, zona horaria, inputs, permisos del propietario y runners antes de activarlas manualmente.

### Código de salida distinto de cero

- `0`: ejecución sin elementos `partial` ni `failed`.
- `1`: fallo global o de preparación.
- `2`: la ejecución terminó con al menos un elemento `partial` o `failed`, incluidas variables heredadas de grupos superiores que requieren intervención.
- `130`: interrupción manual.

Consulta el informe JSON indicado por `--report` o el generado automáticamente para localizar el elemento afectado.

## Seguridad operativa

- Usa HTTPS y valida certificados.
- Emplea tokens temporales, de alcance mínimo y con caducidad corta.
- Revoca los tokens al terminar la migración.
- No captures la terminal ni publiques logs o informes sin revisarlos.
- No actives schedules ni pipelines hasta verificar variables, runners e includes.
- Trata `--existing update` como una operación potencialmente destructiva.
- Evita migrar mientras hay escrituras activas en origen.
- Conserva los informes como evidencia, con acceso restringido.
## Instalación

No hay dependencias de ejecución externas de Python: el migrador utiliza únicamente la biblioteca estándar. Git y, si corresponde, Git LFS sí deben estar instalados como herramientas del sistema.

### Uso directo como script

Clona o descarga el repositorio y ejecuta el archivo desde su raíz:

```powershell
python .\migrate_gitlab_group.py --help
```

Este modo no requiere instalar el paquete.

### Instalación editable opcional

Para desarrollo o para disponer de un comando de consola en un entorno virtual:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --editable .
migrate-gitlab-group --help
```

La instalación editable crea el comando `migrate-gitlab-group`; sus argumentos y comportamiento son los mismos que al ejecutar `migrate_gitlab_group.py` directamente.

## Estructura del repositorio

```text
.
├── migrate_gitlab_group.py   # Implementación y CLI del migrador
├── tests/                     # Pruebas unitarias sin acceso a GitLab real
├── .github/workflows/         # Validación automatizada en GitHub Actions
├── pyproject.toml             # Metadatos y comando instalable
├── CONTRIBUTING.md            # Guía para contribuir
├── SECURITY.md                # Comunicación privada de vulnerabilidades
└── README.md                  # Operación, seguridad y desarrollo
```

Los directorios `tests/` y `.github/workflows/` forman parte de la preparación del repositorio cuando están presentes en la versión consultada.

## Desarrollo y validación

Crea un entorno virtual e instala el proyecto en modo editable siguiendo la sección anterior. La validación local recomendada es:

```powershell
python -m unittest discover -s tests -v
python -m py_compile .\migrate_gitlab_group.py
python .\migrate_gitlab_group.py --help
```

Las pruebas deben usar dobles o respuestas API simuladas; nunca deben depender de tokens ni de instancias reales. Consulta [CONTRIBUTING.md](CONTRIBUTING.md) para los criterios de cambios y pull requests.

## Integración continua

El workflow de GitHub Actions valida el proyecto con versiones compatibles de Python, ejecuta las pruebas unitarias, compila el módulo y comprueba que la ayuda de la CLI se puede generar. La CI no debe conectarse a GitLab reales ni recibir tokens de migración.

## Política de secretos y artefactos

Nunca subas al repositorio:

- `GITLAB_SOURCE_TOKEN` o `GITLAB_TARGET_TOKEN`.
- Archivos `.env`, claves privadas, certificados internos o almacenes de credenciales.
- Checkpoints e informes de migraciones reales.
- Logs con URLs, namespaces, nombres de proyectos o valores internos sin anonimizar.

El archivo `.gitignore` reduce el riesgo de añadir estos elementos por accidente, pero no sustituye la revisión antes de cada commit. Si un secreto llega a versionarse, revócalo y rótalo inmediatamente; eliminarlo del último commit no basta. Para comunicar vulnerabilidades, sigue [SECURITY.md](SECURITY.md).
