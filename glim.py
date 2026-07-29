#!/usr/bin/env python3
"""
Automatiza la migración de repositorios y configuración operativa de CI/CD de un
grupo o subgrupo entre dos instancias de GitLab.

Descubre la jerarquía del grupo origen, la reproduce bajo un grupo destino ya
existente y procesa cada proyecto de forma aislada. Transfiere el contenido con
un mirror para conservar commits, merge commits, ramas y etiquetas; opcionalmente
traslada Git LFS y wikis. También replica variables, ajustes CI/CD compatibles,
protecciones y pipeline schedules accesibles mediante la API.

Sin ``--execute`` solo genera un plan. Durante una ejecución real desactiva CI/CD
mientras carga cada proyecto, crea las schedules inactivas y valida el YAML con
CI Lint. Los tokens proceden exclusivamente de variables de entorno, se envían
por cabeceras y se redactan de errores e informes.

No migra el historial de pipelines, jobs, trazas, artefactos, runners, triggers,
Container Registry, paquetes ni secretos que la API origen no pueda devolver.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("glim")
SOURCE_TOKEN_ENV = "GITLAB_SOURCE_TOKEN"
TARGET_TOKEN_ENV = "GITLAB_TARGET_TOKEN"


class MigrationError(RuntimeError):
    """Representa un fallo operativo esperado durante la migración.

    Unifica errores de API y Git para registrarlos de forma controlada. Según el
    nivel donde se capture, permite continuar con otros proyectos o finalizar la
    ejecución dejando un informe legible.
    """


class ApiError(MigrationError):
    """Especializa un fallo controlado con la respuesta HTTP de GitLab.

    Construye un mensaje uniforme con método, ruta, estado y detalle ya saneado,
    y conserva el código para que pueda evaluarse sin analizar el texto.
    """

    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        """Inicializa un error HTTP uniforme y conserva su código de estado."""
        super().__init__(f"{method} {path}: HTTP {status}: {detail}")
        self.status = status


@dataclass
class ItemResult:
    """Describe el resultado auditable de una unidad migrada.

    Agrupa identidad, estado, detalles, avisos y errores. Así un fallo parcial
    queda asociado a su elemento sin impedir el procesamiento de los demás.
    """

    kind: str
    source: str
    target: str
    status: str
    details: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class MigrationReport:
    """Representa el informe global de una simulación o ejecución.

    Conserva el contexto de ambas instancias y agrega los resultados individuales
    para generar evidencia JSON incluso cuando algunas operaciones fallen.
    """

    mode: str
    source_url: str
    target_url: str
    source_group: str
    target_group: str
    started_at: str = field(default_factory=lambda: utc_now())
    finished_at: str | None = None
    items: list[ItemResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Convierte el informe en datos serializables y calcula su resumen.

        Los contadores se derivan de los elementos para que no se desincronicen ante
        fallos parciales."""
        return {
            "mode": self.mode,
            "source_url": self.source_url,
            "target_url": self.target_url,
            "source_group": self.source_group,
            "target_group": self.target_group,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": {
                "total": len(self.items),
                "ok": sum(item.status == "ok" for item in self.items),
                "planned": sum(item.status == "planned" for item in self.items),
                "skipped": sum(item.status == "skipped" for item in self.items),
                "partial": sum(item.status == "partial" for item in self.items),
                "failed": sum(item.status == "failed" for item in self.items),
            },
            "warnings": self.warnings,
            "errors": self.errors,
            "items": [
                {
                    "kind": item.kind,
                    "source": item.source,
                    "target": item.target,
                    "status": item.status,
                    "details": item.details,
                    "warnings": item.warnings,
                    "errors": item.errors,
                }
                for item in self.items
            ],
        }


def utc_now() -> str:
    """Devuelve la fecha y hora UTC actual en formato ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def encode_path(value: str | int) -> str:
    """Codifica un ID o ruta como un único segmento seguro de URL."""
    return quote(str(value), safe="")


def clean_base_url(value: str) -> str:
    """Elimina barras finales de una URL base para componer rutas correctas."""
    return value.rstrip("/")


def redact(text: str, secrets: Iterable[str]) -> str:
    """Sustituye secretos conocidos antes de mostrar o persistir texto externo.

    Protege especialmente las salidas de HTTP y Git, que podrían repetir datos
    sensibles recibidos en credenciales o respuestas."""
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED_SECRET]")
    return cleaned


class GitLabApi:
    """Cliente REST mínimo para las APIs de origen y destino.

    Centraliza autenticación, JSON, reintentos, redacción y paginación.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: int,
        retries: int,
        role: str,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Configura el cliente para una instancia y su política de reintentos.

        ``role`` distingue origen y destino en mensajes sin revelar el token."""
        self.base_url = clean_base_url(base_url)
        self.api_url = f"{self.base_url}/api/v4"
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.role = role
        self.ssl_context = ssl_context

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        expect_json: bool = True,
        allow_not_found: bool = False,
    ) -> Any:
        """Ejecuta una petición REST y devuelve JSON o bytes.

        Normaliza consultas, serializa datos, reintenta fallos temporales con espera
        progresiva y eleva errores controlados con credenciales redactadas."""
        url = f"{self.api_url}{path}"
        if query:
            normalized = {
                key: str(value).lower() if isinstance(value, bool) else value
                for key, value in query.items()
                if value is not None
            }
            url = f"{url}?{urlencode(normalized, doseq=True)}"

        body = None
        # El token viaja solo en la cabecera y nunca forma parte de la URL.
        headers = {
            "PRIVATE-TOKEN": self.token,
            "Accept": "application/json",
            "User-Agent": "glim/1.0",
        }
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        # Retry-After y el backoff reducen el impacto de fallos transitorios.
        for attempt in range(self.retries + 1):
            request = Request(url, data=body, headers=headers, method=method)
            try:
                with urlopen(
                    request, timeout=self.timeout, context=self.ssl_context
                ) as response:
                    payload = response.read()
                    if not expect_json:
                        return payload
                    return json.loads(payload.decode("utf-8")) if payload else None
            except HTTPError as exc:
                raw_detail = exc.read().decode("utf-8", errors="replace")
                detail = redact(raw_detail[:2000], [self.token])
                if allow_not_found and exc.code == 404:
                    return None
                if exc.code in {429, 502, 503, 504} and attempt < self.retries:
                    retry_after = exc.headers.get("Retry-After")
                    delay = (
                        int(retry_after)
                        if retry_after and retry_after.isdigit()
                        else 2**attempt
                    )
                    LOG.warning(
                        "API %s temporalmente no disponible (HTTP %s); reintento en %ss",
                        self.role,
                        exc.code,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise ApiError(method, path, exc.code, detail) from None
            except URLError as exc:
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                raise MigrationError(
                    f"No se pudo conectar con GitLab {self.role}: {exc.reason}"
                ) from None

        raise AssertionError("Bucle de reintentos agotado de forma inesperada")

    def paginate(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Recorre un endpoint paginado hasta reunir todos sus objetos.

        Solicita lotes de 100 y rechaza respuestas que no sean listas para no ocultar un
        error o un cambio de contrato de la API."""
        page = 1
        results: list[dict[str, Any]] = []
        while True:
            page_query = dict(query or {})
            page_query.update({"page": page, "per_page": 100})
            batch = self.request("GET", path, query=page_query)
            if not isinstance(batch, list):
                raise MigrationError(f"Respuesta paginada inesperada para {path}")
            results.extend(batch)
            if len(batch) < 100:
                return results
            page += 1

    def get_group(self, path_or_id: str | int) -> dict[str, Any] | None:
        """Obtiene un grupo por ID o ruta; devuelve ``None`` ante un 404."""
        return self.request(
            "GET",
            f"/groups/{encode_path(path_or_id)}",
            allow_not_found=True,
        )

    def get_project(self, path_or_id: str | int) -> dict[str, Any] | None:
        """Obtiene un proyecto por ID o ruta; devuelve ``None`` ante un 404."""
        return self.request(
            "GET",
            f"/projects/{encode_path(path_or_id)}",
            allow_not_found=True,
        )


def build_parser() -> argparse.ArgumentParser:
    """Construye la interfaz CLI con valores predeterminados seguros.

    La migración real, la actualización de refs y la sobrescritura de variables
    requieren opciones explícitas por su impacto."""
    parser = argparse.ArgumentParser(
        description=(
            "Migra repositorios y configuración operativa de CI/CD entre dos "
            "grupos de GitLab. Simula por defecto."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source-url", required=True, help="URL del GitLab origen")
    parser.add_argument("--target-url", required=True, help="URL del GitLab destino")
    parser.add_argument(
        "--source-group", required=True, help="Ruta completa del grupo origen"
    )
    parser.add_argument(
        "--target-group", required=True, help="Ruta completa del grupo destino"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Ejecuta cambios; sin esta opción solo se genera el plan",
    )
    parser.add_argument(
        "--include-subgroups",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Incluye subgrupos y conserva la jerarquía relativa",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Incluye proyectos archivados",
    )
    parser.add_argument(
        "--with-lfs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Migra objetos Git LFS cuando git-lfs esté instalado",
    )
    parser.add_argument(
        "--with-wikis",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Migra los repositorios wiki que existan",
    )
    parser.add_argument(
        "--existing",
        choices=("skip", "update"),
        default="skip",
        help=(
            "Tratamiento de proyectos ya existentes. 'update' puede reemplazar "
            "y eliminar referencias Git en destino"
        ),
    )
    parser.add_argument(
        "--overwrite-ci-variables",
        action="store_true",
        help="Actualiza variables CI/CD que ya existan en destino",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Ruta del informe JSON; si se omite se crea con fecha y hora",
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="Timeout HTTP en segundos"
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="Reintentos para errores temporales"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reanuda fases registradas en el checkpoint sin adoptar proyectos ajenos",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=Path("glim-checkpoint.json"),
        help="Estado durable por proyecto y fase para reanudar con seguridad",
    )
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="Permite HTTP sin TLS; desaconsejado salvo redes aisladas",
    )
    parser.add_argument(
        "--ca-bundle",
        type=Path,
        help="CA PEM adicional común para ambas instancias; conserva las CA del sistema",
    )
    parser.add_argument(
        "--source-ca-bundle",
        type=Path,
        help="CA PEM adicional aplicada solo al GitLab origen",
    )
    parser.add_argument(
        "--target-ca-bundle",
        type=Path,
        help="CA PEM adicional aplicada solo al GitLab destino",
    )
    parser.add_argument(
        "--git-timeout",
        type=int,
        default=1800,
        help="Timeout de cada operación Git o Git LFS en segundos",
    )
    parser.add_argument(
        "--min-free-space-mb",
        type=int,
        default=1024,
        help="Espacio libre mínimo exigido para temporales antes de mutar",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Muestra información adicional sin revelar secretos",
    )
    return parser


class Migrator:
    """Orquesta la migración completa de un grupo entre dos GitLab.

    Mantiene el mapeo de namespaces, coordina API y Git, y procesa cada proyecto
    de forma aislada para continuar tras fallos dejando trazabilidad en el informe.
    """

    def __init__(
        self, args: argparse.Namespace, source_token: str, target_token: str
    ) -> None:
        """Prepara clientes, mapeos e informe sin modificar GitLab.

        Guarda ambos tokens en la lista de secretos usada para redactar cualquier salida."""
        self.args = args
        self.execute = args.execute
        self.source_token = source_token
        self.target_token = target_token
        self.secrets = [source_token, target_token]
        self.source_ca_bundle = args.source_ca_bundle or args.ca_bundle
        self.target_ca_bundle = args.target_ca_bundle or args.ca_bundle
        self.source_ssl_context = ssl.create_default_context()
        self.target_ssl_context = ssl.create_default_context()
        if self.source_ca_bundle:
            self.source_ssl_context.load_verify_locations(
                cafile=str(self.source_ca_bundle.resolve())
            )
        if self.target_ca_bundle:
            self.target_ssl_context.load_verify_locations(
                cafile=str(self.target_ca_bundle.resolve())
            )
        self.source = GitLabApi(
            args.source_url,
            source_token,
            timeout=args.timeout,
            retries=args.retries,
            role="origen",
            ssl_context=self.source_ssl_context,
        )
        self.target = GitLabApi(
            args.target_url,
            target_token,
            timeout=args.timeout,
            retries=args.retries,
            role="destino",
            ssl_context=self.target_ssl_context,
        )
        self.source_root: dict[str, Any] = {}
        self.target_root: dict[str, Any] = {}
        self.group_map: dict[str, dict[str, Any]] = {}
        self.report = MigrationReport(
            mode="execute" if self.execute else "dry-run",
            source_url=clean_base_url(args.source_url),
            target_url=clean_base_url(args.target_url),
            source_group=args.source_group.strip("/"),
            target_group=args.target_group.strip("/"),
        )
        self.git_lfs_available = False
        self.checkpoint_path = args.checkpoint
        self.checkpoint: dict[str, Any] = {"version": 2, "projects": {}}
        self.pending_ci_validation: list[
            tuple[dict[str, Any], dict[str, Any], ItemResult]
        ] = []
        self.pending_job_token_scope: list[
            tuple[dict[str, Any], dict[str, Any], ItemResult]
        ] = []
        self.pending_archives: list[tuple[dict[str, Any], ItemResult]] = []
        self.source_projects: list[dict[str, Any]] = []

    def run(self) -> MigrationReport:
        """Descubre y valida todo el ámbito antes de efectuar la primera mutación."""
        self.preflight()
        source_groups = self.discover_groups()
        self.source_projects = [
            self.source.get_project(project["id"]) or project
            for project in self.discover_projects()
        ]
        self.validate_migration_scope(source_groups, self.source_projects)
        self.inventory_inherited_group_variables()
        self.ensure_target_groups(source_groups)
        self.copy_group_variables(source_groups)

        LOG.info("Se procesarán %d proyectos", len(self.source_projects))
        for index, project in enumerate(self.source_projects, start=1):
            LOG.info(
                "[%d/%d] %s",
                index,
                len(self.source_projects),
                project["path_with_namespace"],
            )
            self.migrate_project(project)

        # Las allowlists solo se pueden mapear cuando todos los destinos existen.
        for source_project, target_project, result in self.pending_job_token_scope:
            if not self.phase_completed(result.source, "job_token_scope"):
                before = len(result.errors)
                self.copy_job_token_scope(source_project, target_project, result)
                if len(result.errors) == before:
                    self.checkpoint_phase(
                        result.source, "job_token_scope", target_project
                    )

        # Revalida refs al final para detectar cambios en origen durante una reanudación.
        # Los includes entre proyectos solo pueden resolverse con todo el grupo cargado.
        for source_project, target_project, result in self.pending_ci_validation:
            try:
                self.verify_repository(source_project, target_project, result)
                result.details.append("Referencias Git verificadas de nuevo al cierre")
            except Exception as exc:
                self.clear_checkpoint_phases(result.source, {"repository", "complete"})
                self.mark_partial(
                    result,
                    "Verificación Git final fallida: " + redact(str(exc), self.secrets),
                )
            try:
                self.validate_ci(target_project, result)
            except Exception as exc:
                self.mark_partial(
                    result,
                    "Validación CI final fallida: " + redact(str(exc), self.secrets),
                )
        # Archivar es la última mutación: job-token scope y validaciones necesitan
        # que el proyecto siga editable durante toda la migración.
        for target_project, result in self.pending_archives:
            if not self.phase_completed(result.source, "archived"):
                try:
                    self.target.request(
                        "POST", f"/projects/{target_project['id']}/archive"
                    )
                    self.checkpoint_phase(result.source, "archived", target_project)
                    result.details.append("Proyecto archivado como en origen")
                except Exception as exc:
                    self.mark_partial(
                        result,
                        "No se pudo archivar el proyecto: "
                        + redact(str(exc), self.secrets),
                    )

        self.report.finished_at = utc_now()
        return self.report

    def checkpoint_identity(self) -> dict[str, Any]:
        """Identifica el ámbito y las opciones que cambian el resultado durable."""
        return {
            "source_url": clean_base_url(self.args.source_url),
            "target_url": clean_base_url(self.args.target_url),
            "source_group": self.args.source_group.strip("/"),
            "target_group": self.args.target_group.strip("/"),
            "options": {
                "include_subgroups": self.args.include_subgroups,
                "include_archived": self.args.include_archived,
                "with_lfs": self.args.with_lfs,
                "with_wikis": self.args.with_wikis,
                "existing": self.args.existing,
                "overwrite_ci_variables": self.args.overwrite_ci_variables,
            },
        }

    def load_checkpoint(self) -> None:
        """Carga un checkpoint solo cuando se solicita reanudación explícita."""
        if not self.args.resume:
            return
        if not self.checkpoint_path.exists():
            raise MigrationError(
                f"No existe el checkpoint para --resume: {self.checkpoint_path}"
            )
        try:
            loaded = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationError(f"Checkpoint ilegible: {exc}") from exc
        identity = loaded.get("identity", {})
        expected = self.checkpoint_identity()
        if identity != expected:
            raise MigrationError("El checkpoint pertenece a otra migración")
        self.checkpoint = loaded

    def save_checkpoint(self) -> None:
        """Persiste atómicamente el avance sin credenciales ni valores CI/CD."""
        if not self.execute:
            return
        self.checkpoint["version"] = 2
        self.checkpoint["identity"] = self.checkpoint_identity()
        self.checkpoint["updated_at"] = utc_now()
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint_path.with_suffix(
            self.checkpoint_path.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(self.checkpoint, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.checkpoint_path)

    def checkpoint_phase(
        self,
        source_path: str,
        phase: str,
        target_project: dict[str, Any],
        *,
        created_by_migrator: bool | None = None,
        migration_mode: str | None = None,
    ) -> None:
        """Marca una fase completada y la identidad exacta de su proyecto destino."""
        entry = self.checkpoint.setdefault("projects", {}).setdefault(source_path, {})
        entry.update(
            {
                "target_id": target_project["id"],
                "target_path": target_project["path_with_namespace"],
                "phase": phase,
            }
        )
        completed = entry.setdefault("completed_phases", [])
        if phase not in completed:
            completed.append(phase)
        if created_by_migrator is not None:
            entry["created_by_migrator"] = created_by_migrator
        if migration_mode is not None:
            entry["migration_mode"] = migration_mode
        self.save_checkpoint()

    def phase_completed(self, source_path: str, phase: str) -> bool:
        """Consulta fases independientes; acepta checkpoints lineales de versiones previas."""
        entry = self.checkpoint.get("projects", {}).get(source_path, {})
        completed = entry.get("completed_phases")
        if isinstance(completed, list):
            return phase in completed
        phases = [
            "created",
            "repository",
            "variables",
            "settings",
            "deploy_keys",
            "protections",
            "schedules",
            "restored",
            "protected_environments",
            "wiki",
            "job_token_scope",
            "complete",
            "archived",
        ]
        current = entry.get("phase")
        return current in phases and phases.index(current) >= phases.index(phase)

    def clear_checkpoint_phases(self, source_path: str, phases: set[str]) -> None:
        """Invalida fases que deben repetirse tras detectar deriva o corrupción."""
        if not self.execute:
            return
        entry = self.checkpoint.get("projects", {}).get(source_path)
        if not entry:
            return
        completed = entry.get("completed_phases")
        if isinstance(completed, list):
            entry["completed_phases"] = [
                item for item in completed if item not in phases
            ]
        if entry.get("phase") in phases:
            entry["phase"] = "invalidated"
        self.save_checkpoint()

    def preflight(self) -> None:
        """Completa controles de seguridad antes de crear grupos o proyectos."""
        source_url = clean_base_url(self.args.source_url)
        target_url = clean_base_url(self.args.target_url)
        for role, value in (("origen", source_url), ("destino", target_url)):
            parsed = urlsplit(value)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc:
                raise MigrationError(f"URL {role} no válida: {value}")
            if parsed.scheme != "https" and not self.args.allow_http:
                raise MigrationError(
                    f"La URL {role} usa HTTP; añada --allow-http solo si es intencionado"
                )
        if source_url.lower() == target_url.lower():
            raise MigrationError("Las URLs de origen y destino deben ser diferentes")
        if self.args.ca_bundle and (
            self.args.source_ca_bundle or self.args.target_ca_bundle
        ):
            raise MigrationError(
                "--ca-bundle no se combina con --source-ca-bundle/--target-ca-bundle; elija un modo"
            )
        for label, bundle in (
            ("común", self.args.ca_bundle),
            ("origen", self.args.source_ca_bundle),
            ("destino", self.args.target_ca_bundle),
        ):
            if bundle and not bundle.is_file():
                raise MigrationError(f"No existe el CA bundle {label}: {bundle}")
        if self.execute and self.checkpoint_path.exists() and not self.args.resume:
            raise MigrationError(
                f"Ya existe el checkpoint {self.checkpoint_path}; use --resume o elija otra ruta"
            )
        if (
            self.args.timeout <= 0
            or self.args.git_timeout <= 0
            or self.args.min_free_space_mb < 0
        ):
            raise MigrationError(
                "Los timeouts deben ser positivos y el espacio mínimo no negativo"
            )
        free_mb = shutil.disk_usage(tempfile.gettempdir()).free // (1024 * 1024)
        if free_mb < self.args.min_free_space_mb:
            raise MigrationError(
                f"Espacio temporal insuficiente: {free_mb} MiB libres; se exigen {self.args.min_free_space_mb} MiB"
            )
        self.load_checkpoint()
        self.source_root = self.source.get_group(self.args.source_group) or {}
        if not self.source_root:
            raise MigrationError(
                f"No existe o no es visible el grupo origen: {self.args.source_group}"
            )
        self.target_root = self.target.get_group(self.args.target_group) or {}
        if not self.target_root:
            raise MigrationError(
                f"El grupo raíz destino debe existir previamente: {self.args.target_group}"
            )
        self.group_map[self.source_root["full_path"]] = self.target_root
        self._check_tool(["git", "--version"], "Git")
        self.git_lfs_available = self._tool_available(["git", "lfs", "version"])
        if self.args.with_lfs and not self.git_lfs_available:
            message = "Se solicitó LFS pero git-lfs no está instalado"
            if self.execute:
                raise MigrationError(
                    message + "; use --no-with-lfs solo si acepta no migrar sus objetos"
                )
            self.report.warnings.append(message + "; la simulación continúa")
        source_version = self.source.request("GET", "/version")
        target_version = self.target.request("GET", "/version")
        LOG.info(
            "GitLab origen %s; destino %s",
            source_version.get("version", "desconocido"),
            target_version.get("version", "desconocido"),
        )

    def validate_migration_scope(
        self,
        groups: list[dict[str, Any]],
        projects: list[dict[str, Any]],
    ) -> None:
        """Valida rutas, detalles y formatos sin mutar el destino."""
        if self.source_root["full_path"] == self.target_root["full_path"] and (
            urlsplit(self.args.source_url).netloc.lower()
            == urlsplit(self.args.target_url).netloc.lower()
        ):
            raise MigrationError("Origen y destino resuelven al mismo namespace")
        group_paths = {group["full_path"] for group in groups}
        for group in groups:
            self.source.paginate(f"/groups/{group['id']}/variables")
        for project in projects:
            namespace = project.get("namespace", {}).get("full_path")
            if namespace not in group_paths:
                raise MigrationError(
                    f"Proyecto fuera del ámbito resuelto: {project.get('path_with_namespace')}"
                )
            target_path = f"{self.target_group_path(namespace)}/{project['path']}"
            if (
                target_path == project["path_with_namespace"]
                and urlsplit(self.args.source_url).netloc.lower()
                == urlsplit(self.args.target_url).netloc.lower()
            ):
                raise MigrationError(f"Autodestino detectado: {target_path}")
            object_format = project.get("repository_object_format", "sha1")
            if object_format not in {"sha1", "sha256"}:
                raise MigrationError(
                    f"Formato Git no soportado en {project['path_with_namespace']}: {object_format}"
                )
            existing_target = self.target.get_project(target_path)
            entry = self.checkpoint.get("projects", {}).get(
                project["path_with_namespace"], {}
            )
            if self.args.resume and entry:
                if not existing_target:
                    raise MigrationError(
                        f"El destino registrado en checkpoint fue eliminado: {target_path}"
                    )
                if entry.get("target_id") != existing_target.get("id") or entry.get(
                    "target_path"
                ) != existing_target.get("path_with_namespace"):
                    raise MigrationError(
                        f"Cambió la identidad del destino registrado: {target_path}"
                    )
                allowed_resume = bool(entry.get("created_by_migrator")) or bool(
                    entry.get("migration_mode") == "existing_update"
                    and self.args.existing == "update"
                )
                if not allowed_resume:
                    raise MigrationError(
                        f"Checkpoint no reanudable para el destino: {target_path}"
                    )
            elif (
                self.args.resume and existing_target and self.args.existing == "update"
            ):
                raise MigrationError(
                    f"--resume no adopta un destino sin entrada exacta de checkpoint: {target_path}"
                )
            if existing_target:
                target_format = existing_target.get("repository_object_format", "sha1")
                if target_format != object_format:
                    raise MigrationError(
                        f"Formato Git incompatible en destino {target_path}: {object_format} -> {target_format}"
                    )
                if (
                    existing_target.get("id") == project.get("id")
                    and urlsplit(self.args.source_url).netloc.lower()
                    == urlsplit(self.args.target_url).netloc.lower()
                ):
                    raise MigrationError(
                        f"Proyecto origen y destino tienen la misma identidad: {target_path}"
                    )
            if project.get("lfs_enabled") and not self.args.with_lfs:
                self.report.warnings.append(
                    f"{project['path_with_namespace']}: LFS está habilitado pero se ha desactivado su migración"
                )
            # Estos GET anticipan permisos insuficientes antes de la primera escritura.
            for endpoint in (
                "variables",
                "protected_branches",
                "protected_tags",
                "pipeline_schedules",
            ):
                values = self.source.paginate(f"/projects/{project['id']}/{endpoint}")
                if endpoint == "pipeline_schedules":
                    for schedule in values:
                        self.source.request(
                            "GET",
                            f"/projects/{project['id']}/pipeline_schedules/{schedule['id']}",
                        )
            # Funciones dependientes de tier/versión se inventarían sin abortar el
            # grupo completo; durante la ejecución se convertirán en partial.
            try:
                self.source.paginate(f"/projects/{project['id']}/deploy_keys")
                self.source.paginate(
                    f"/projects/{project['id']}/protected_environments"
                )
                self.source.request("GET", f"/projects/{project['id']}/job_token_scope")
                self.source.paginate(
                    f"/projects/{project['id']}/job_token_scope/allowlist"
                )
                self.source.paginate(
                    f"/projects/{project['id']}/job_token_scope/groups_allowlist"
                )
            except MigrationError as exc:
                self.report.warnings.append(
                    f"{project['path_with_namespace']}: preflight CI/CD avanzado incompleto: "
                    + redact(str(exc), self.secrets)
                )

    def inventory_inherited_group_variables(self) -> None:
        """Inventaría variables de grupos superiores, que no pertenecen al alcance de copia."""
        parent_id = self.source_root.get("parent_id")
        while parent_id:
            parent = self.source.get_group(parent_id)
            if not parent:
                break
            variables = self.source.paginate(f"/groups/{parent['id']}/variables")
            if variables:
                item = ItemResult(
                    "inherited-group-ci-variables",
                    parent["full_path"],
                    self.target_root["full_path"],
                    "partial",
                    details=[
                        f"{len(variables)} variables heredables inventariadas; no se copian fuera del ámbito"
                    ],
                    warnings=[
                        "Revise colisiones y replíquelas en un ancestro equivalente del destino"
                    ],
                )
                self.report.items.append(item)
            parent_id = parent.get("parent_id")

    def _check_tool(self, command: list[str], name: str) -> None:
        """Exige que una herramienta externa esté disponible o aborta."""
        if not self._tool_available(command):
            raise MigrationError(f"No se encontró {name} o no se puede ejecutar")

    @staticmethod
    def _tool_available(command: list[str]) -> bool:
        """Comprueba silenciosamente si un comando externo se puede ejecutar."""
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
            return completed.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def discover_groups(self) -> list[dict[str, Any]]:
        """Descubre la raíz y, opcionalmente, todos sus subgrupos.

        Ordena por profundidad para resolver cada padre antes que sus descendientes."""
        groups = [self.source_root]
        if self.args.include_subgroups:
            groups.extend(
                self.source.paginate(
                    f"/groups/{self.source_root['id']}/descendant_groups",
                    query={"order_by": "id", "sort": "asc"},
                )
            )
        return sorted(groups, key=lambda item: item["full_path"].count("/"))

    def discover_projects(self) -> list[dict[str, Any]]:
        """Enumera los proyectos del ámbito solicitado.

        Excluye compartidos y normalmente archivados; el filtro de namespace impide
        aceptar resultados externos por diferencias entre versiones de GitLab."""
        projects = self.source.paginate(
            f"/groups/{self.source_root['id']}/projects",
            query={
                "include_subgroups": self.args.include_subgroups,
                "with_shared": False,
                "archived": None if self.args.include_archived else False,
                "order_by": "path",
                "sort": "asc",
            },
        )
        root_path = self.source_root["full_path"]
        return [
            project
            for project in projects
            if project["path_with_namespace"] == root_path
            or project["path_with_namespace"].startswith(f"{root_path}/")
        ]

    def target_group_path(self, source_group_path: str) -> str:
        """Conserva la ruta relativa del namespace bajo el grupo destino."""
        source_root_path = self.source_root["full_path"]
        target_root_path = self.target_root["full_path"]
        if source_group_path == source_root_path:
            return target_root_path
        relative = source_group_path[len(source_root_path) :].lstrip("/")
        return f"{target_root_path}/{relative}"

    def ensure_target_groups(self, source_groups: list[dict[str, Any]]) -> None:
        """Resuelve o crea en destino cada subgrupo descubierto.

        En simulación usa grupos sintéticos en memoria; en ejecución avanza de padres a
        hijos y replica los atributos básicos compatibles."""
        for source_group in source_groups:
            source_path = source_group["full_path"]
            if source_path == self.source_root["full_path"]:
                continue
            target_path = self.target_group_path(source_path)
            existing = self.target.get_group(target_path)
            if existing:
                self.group_map[source_path] = existing
                self.report.items.append(
                    ItemResult(
                        "group", source_path, target_path, "skipped", ["Ya existe"]
                    )
                )
                continue

            parent_source_path = source_path.rsplit("/", 1)[0]
            parent_target = self.group_map.get(parent_source_path)
            if not parent_target:
                raise MigrationError(
                    f"No se pudo resolver el grupo padre de {source_path}"
                )

            if not self.execute:
                synthetic = {
                    "id": f"dry-run:{target_path}",
                    "full_path": target_path,
                    "path": source_group["path"],
                }
                self.group_map[source_path] = synthetic
                self.report.items.append(
                    ItemResult(
                        "group", source_path, target_path, "planned", ["Se crearía"]
                    )
                )
                continue

            created = self.target.request(
                "POST",
                "/groups",
                data={
                    "name": source_group["name"],
                    "path": source_group["path"],
                    "parent_id": parent_target["id"],
                    "visibility": source_group.get("visibility", "private"),
                    "description": source_group.get("description") or "",
                },
            )
            self.group_map[source_path] = created
            self.report.items.append(
                ItemResult("group", source_path, target_path, "ok", ["Creado"])
            )

    def copy_group_variables(self, source_groups: list[dict[str, Any]]) -> None:
        """Planifica o copia variables CI/CD para cada grupo y subgrupo.

        Registra un resultado auditable por namespace y delega duplicados y redacción."""
        for source_group in source_groups:
            target_group = self.group_map[source_group["full_path"]]
            variables = self.source.paginate(f"/groups/{source_group['id']}/variables")
            if not variables:
                continue
            result = ItemResult(
                "group-ci-variables",
                source_group["full_path"],
                target_group["full_path"],
                "planned" if not self.execute else "ok",
            )
            if not self.execute:
                result.details.append(f"Se copiarían {len(variables)} variables")
            else:
                copied, skipped, failed = self.copy_variables(
                    variables,
                    f"/groups/{target_group['id']}/variables",
                    result,
                )
                result.details.append(
                    f"Copiadas: {copied}; omitidas: {skipped}; fallidas: {failed}"
                )
                if failed:
                    result.status = "failed"
            self.report.items.append(result)

    def copy_variables(
        self,
        variables: list[dict[str, Any]],
        target_endpoint: str,
        result: ItemResult,
    ) -> tuple[int, int, int]:
        """Copia variables CI/CD respetando ámbito, atributos y duplicados.

        Identifica por clave y ``environment_scope``; solo sobrescribe con autorización
        y aísla cada error redactando tokens y valor. Devuelve sus tres contadores."""
        existing = self.target.paginate(target_endpoint)
        existing_map = {
            (item.get("key"), item.get("environment_scope", "*")): item
            for item in existing
        }
        existing_keys = set(existing_map)
        copied = skipped = failed = 0
        for variable in variables:
            key = variable["key"]
            environment_scope = variable.get("environment_scope", "*")
            identity = (key, environment_scope)
            desired_hidden = bool(
                variable.get("hidden") or variable.get("masked_and_hidden")
            )
            payload = {
                field: variable[field]
                for field in (
                    "key",
                    "value",
                    "variable_type",
                    "protected",
                    "masked",
                    "raw",
                    "environment_scope",
                    "description",
                )
                if field in variable and variable[field] is not None
            }
            try:
                if identity in existing_keys:
                    current = existing_map[identity]
                    if bool(current.get("hidden")) != desired_hidden:
                        skipped += 1
                        self.mark_partial(
                            result,
                            f"No se puede cambiar con seguridad la visibilidad hidden de {key} [{environment_scope}]",
                        )
                        continue
                    if not self.args.overwrite_ci_variables:
                        skipped += 1
                        comparable = (
                            "value",
                            "variable_type",
                            "protected",
                            "masked",
                            "raw",
                            "description",
                        )
                        if any(
                            current.get(field) != payload.get(field)
                            for field in comparable
                            if field in payload
                        ):
                            self.mark_partial(
                                result,
                                f"Variable existente distinta y no sobrescrita: {key} [{environment_scope}]",
                            )
                        else:
                            result.details.append(
                                f"Variable existente equivalente: {key} [{environment_scope}]"
                            )
                        continue
                    filter_query = {"filter[environment_scope]": environment_scope}
                    self.target.request(
                        "PUT",
                        f"{target_endpoint}/{encode_path(key)}",
                        query=filter_query,
                        data=payload,
                    )
                else:
                    if desired_hidden:
                        # La API de creación usa masked_and_hidden; ``hidden`` es
                        # un atributo de lectura y no debe enviarse directamente.
                        payload["masked_and_hidden"] = True
                    self.target.request("POST", target_endpoint, data=payload)
                copied += 1
            except MigrationError as exc:
                failed += 1
                result.errors.append(
                    f"No se pudo copiar {key} [{environment_scope}]: "
                    f"{redact(str(exc), self.secrets + [str(variable.get('value', ''))])}"
                )
        return copied, skipped, failed

    def mark_partial(
        self, result: ItemResult, message: str, *, error: bool = True
    ) -> None:
        """Marca un componente obligatorio incompleto sin ocultar que Git sí pudo migrarse."""
        if result.status != "failed":
            result.status = "partial"
        (result.errors if error else result.warnings).append(message)

    def migrate_project(self, source_project: dict[str, Any]) -> None:
        """Migra por fases, reanuda solo destinos propios y restaura CI en ``finally``."""
        source_path = source_project["path_with_namespace"]
        source_namespace = source_project["namespace"]["full_path"]
        target_group = self.group_map.get(source_namespace)
        target_path = (
            f"{target_group['full_path']}/{source_project['path']}"
            if target_group
            else self.target_group_path(source_namespace) + f"/{source_project['path']}"
        )
        result = ItemResult("project", source_path, target_path, "planned")
        self.report.items.append(result)
        target_project: dict[str, Any] | None = None
        restore_source: dict[str, Any] | None = None
        try:
            if not target_group:
                raise MigrationError(
                    f"No existe el mapeo del namespace {source_namespace}"
                )
            target_project = self.target.get_project(target_path)
            entry = self.checkpoint.get("projects", {}).get(source_path, {})
            resumable = bool(
                self.args.resume
                and target_project
                and entry.get("target_id") == target_project.get("id")
                and entry.get("target_path")
                == target_project.get("path_with_namespace")
                and (
                    entry.get("created_by_migrator")
                    or (
                        entry.get("migration_mode") == "existing_update"
                        and self.args.existing == "update"
                    )
                )
            )
            if target_project and self.args.resume and not resumable:
                if self.args.existing == "skip" and not entry:
                    result.status = "skipped"
                    result.details.append(
                        "Proyecto preexistente omitido; no se adopta en checkpoint"
                    )
                    return
                raise MigrationError(
                    "El proyecto destino no está identificado por este checkpoint; no se adopta"
                )
            if target_project and not resumable and self.args.existing == "skip":
                result.status = "skipped"
                result.details.append("El proyecto ya existe en destino")
                return
            if not self.execute:
                result.details.extend(
                    self.describe_project_plan(source_project, bool(target_project))
                )
                return
            if not target_project:
                target_project = self.create_target_project(
                    source_project, target_group
                )
                restore_source = source_project
                self.checkpoint_phase(
                    source_path,
                    "created",
                    target_project,
                    created_by_migrator=True,
                    migration_mode="created",
                )
                result.details.append("Proyecto creado e identificado en checkpoint")
            else:
                restore_source = source_project
                if not resumable:
                    result.warnings.append(
                        "Se actualiza un proyecto existente; push --mirror puede eliminar refs exclusivas"
                    )
                    self.checkpoint_phase(
                        source_path,
                        "created",
                        target_project,
                        created_by_migrator=False,
                        migration_mode="existing_update",
                    )
            self.target.request(
                "PUT",
                f"/projects/{target_project['id']}",
                data={"builds_access_level": "disabled"},
            )
            if not self.phase_completed(source_path, "repository"):
                before = len(result.errors)
                self.copy_repository(source_project, target_project, result)
                if len(result.errors) == before:
                    self.checkpoint_phase(source_path, "repository", target_project)
            if not self.phase_completed(source_path, "variables"):
                before = len(result.errors)
                self.copy_project_variables(source_project, target_project, result)
                if len(result.errors) == before:
                    self.checkpoint_phase(source_path, "variables", target_project)
            if not self.phase_completed(source_path, "settings"):
                before = len(result.errors)
                self.copy_project_ci_settings(source_project, target_project, result)
                if len(result.errors) == before:
                    self.checkpoint_phase(source_path, "settings", target_project)
            if not self.phase_completed(source_path, "deploy_keys"):
                before = len(result.errors)
                self.copy_deploy_keys(source_project, target_project, result)
                if len(result.errors) == before:
                    self.checkpoint_phase(source_path, "deploy_keys", target_project)
            if not self.phase_completed(source_path, "protections"):
                before = len(result.errors)
                self.copy_protected_refs(source_project, target_project, result)
                if len(result.errors) == before:
                    self.checkpoint_phase(source_path, "protections", target_project)
            if not self.phase_completed(source_path, "schedules"):
                before = len(result.errors)
                self.copy_pipeline_schedules(source_project, target_project, result)
                if len(result.errors) == before:
                    self.checkpoint_phase(source_path, "schedules", target_project)
            if (
                self.args.with_wikis
                and source_project.get("wiki_enabled", True)
                and not self.phase_completed(source_path, "wiki")
            ):
                self.copy_wiki(source_project, target_project, result)
                self.checkpoint_phase(source_path, "wiki", target_project)
            if result.status == "planned":
                result.status = "ok"
        except Exception as exc:
            message = redact(str(exc), self.secrets)
            result.status = "failed"
            result.errors.append(message)
            LOG.error("%s: %s", source_path, message)
        finally:
            if self.execute and target_project and restore_source:
                try:
                    self.restore_project_state(restore_source, target_project, result)
                    self.checkpoint_phase(source_path, "restored", target_project)
                    if not self.phase_completed(source_path, "protected_environments"):
                        before = len(result.errors)
                        self.copy_protected_environments(
                            restore_source, target_project, result
                        )
                        if len(result.errors) == before:
                            self.checkpoint_phase(
                                source_path, "protected_environments", target_project
                            )
                except Exception as restore_exc:
                    self.mark_partial(
                        result,
                        "No se pudo restaurar o completar la protección CI/CD: "
                        + redact(str(restore_exc), self.secrets),
                    )
            if self.execute and target_project and result.status in {"ok", "partial"}:
                self.pending_job_token_scope.append(
                    (source_project, target_project, result)
                )
                self.pending_ci_validation.append(
                    (source_project, target_project, result)
                )
                if source_project.get("archived"):
                    self.pending_archives.append((target_project, result))

    def describe_project_plan(
        self,
        source_project: dict[str, Any],
        target_exists: bool,
    ) -> list[str]:
        """Enumera las acciones previstas para un proyecto en simulación."""
        action = (
            "Actualizaría el proyecto existente"
            if target_exists
            else "Crearía el proyecto"
        )
        details = [
            action,
            "Migraría todas las referencias Git",
            "Copiaría variables y ajustes CI/CD",
            "Copiaría deploy keys públicas antes de las protecciones",
            "Crearía schedules inactivas",
            "Copiaría protecciones de refs y entornos protegidos compatibles",
            "Reconciliaría CI_JOB_TOKEN scope y validaría CI Lint",
        ]
        if self.args.with_lfs:
            details.append("Migraría Git LFS si está disponible")
        if self.args.with_wikis:
            details.append("Intentaría migrar la wiki")
        return details

    def create_target_project(
        self,
        source_project: dict[str, Any],
        target_group: dict[str, Any],
    ) -> dict[str, Any]:
        """Crea un proyecto vacío con CI/CD desactivado.

        No genera README para evitar referencias divergentes antes del primer mirror."""
        return self.target.request(
            "POST",
            "/projects",
            data={
                "name": source_project["name"],
                "path": source_project["path"],
                "namespace_id": target_group["id"],
                "description": source_project.get("description") or "",
                "visibility": source_project.get("visibility", "private"),
                "initialize_with_readme": False,
                "builds_access_level": "disabled",
                "lfs_enabled": source_project.get("lfs_enabled", True),
                "repository_object_format": source_project.get(
                    "repository_object_format", "sha1"
                ),
            },
        )

    def git_env(self, token: str, role: str) -> dict[str, str]:
        """Acota autenticación y CA al host explícito, incluso con tokens iguales."""
        env = os.environ.copy()
        if role not in {"source", "target"}:
            raise MigrationError(f"Rol Git no válido: {role}")
        base_url = self.args.source_url if role == "source" else self.args.target_url
        ca_bundle = self.source_ca_bundle if role == "source" else self.target_ca_bundle
        parsed = urlsplit(clean_base_url(base_url))
        scope = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        credentials = base64.b64encode(f"oauth2:{token}".encode()).decode("ascii")
        entries = [(f"http.{scope}.extraHeader", f"Authorization: Basic {credentials}")]
        if ca_bundle:
            entries.append((f"http.{scope}.sslCAInfo", str(ca_bundle.resolve())))
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_COUNT"] = str(len(entries))
        for index, (key, value) in enumerate(entries):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        return env

    def run_git(
        self,
        command: list[str],
        *,
        cwd: Path | None,
        token: str,
        role: str,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Ejecuta Git capturando y redactando su salida.

        Convierte fallos obligatorios en ``MigrationError``; ``allow_failure`` permite
        tratar LFS o wiki como operaciones opcionales."""
        display = " ".join(command)
        LOG.debug("Ejecutando: %s", display)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=self.git_env(token, role),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.args.git_timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise MigrationError(
                f"Timeout de {self.args.git_timeout}s ejecutando {display}"
            ) from exc
        if completed.returncode and not allow_failure:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise MigrationError(
                f"Falló {display}: {redact(detail[-4000:], self.secrets)}"
            )
        return completed

    def copy_repository(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> str | None:
        """Copia todas las referencias Git y, si procede, objetos LFS.

        ``clone --mirror`` y ``push --mirror`` conservan ramas, etiquetas, commits y
        merges. LFS se procesa aparte porque sus objetos no pertenecen al grafo Git."""
        if source_project.get("empty_repo"):
            result.details.append(
                "Repositorio vacío; no hay referencias Git que copiar"
            )
            return None

        source_url = source_project["http_url_to_repo"]
        target_url = target_project["http_url_to_repo"]
        with tempfile.TemporaryDirectory(prefix="glim-") as temp_dir:
            repo_dir = Path(temp_dir) / "repository.git"
            self.run_git(
                ["git", "clone", "--mirror", source_url, str(repo_dir)],
                cwd=None,
                token=self.source_token,
                role="source",
            )
            ci_content = self.read_ci_config(repo_dir, source_project, result)
            self.run_git(
                ["git", "remote", "add", "destination", target_url],
                cwd=repo_dir,
                token=self.target_token,
                role="target",
            )

            if self.args.with_lfs and self.git_lfs_available:
                fetch = self.run_git(
                    ["git", "lfs", "fetch", "--all", "origin"],
                    cwd=repo_dir,
                    token=self.source_token,
                    role="source",
                    allow_failure=True,
                )
                if fetch.returncode == 0:
                    push = self.run_git(
                        ["git", "lfs", "push", "--all", "destination"],
                        cwd=repo_dir,
                        token=self.target_token,
                        role="target",
                        allow_failure=True,
                    )
                    if push.returncode:
                        self.mark_partial(
                            result,
                            "Git LFS no se pudo enviar: "
                            + redact(push.stderr.strip()[-1000:], self.secrets),
                        )
                    else:
                        result.details.append("Objetos Git LFS procesados")
                else:
                    self.mark_partial(
                        result,
                        "Git LFS no se pudo descargar: "
                        + redact(fetch.stderr.strip()[-1000:], self.secrets),
                    )

            # El mirror puede borrar refs exclusivas del destino; actualizar un
            # proyecto existente requiere --existing update de forma explícita.
            self.run_git(
                ["git", "push", "--mirror", "destination"],
                cwd=repo_dir,
                token=self.target_token,
                role="target",
            )
            self.verify_repository(source_project, target_project, result)
            result.details.append("Referencias Git migradas y verificadas por SHA")
            return ci_content

    def verify_repository(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> None:
        """Compara todas las refs visibles, formato de objetos y rama predeterminada."""

        def refs(url: str, token: str, role: str) -> dict[str, str]:
            """Obtiene las referencias visibles y sus SHA mediante git ls-remote."""
            completed = self.run_git(
                ["git", "ls-remote", "--refs", url], cwd=None, token=token, role=role
            )
            output: dict[str, str] = {}
            for line in completed.stdout.splitlines():
                if "\t" in line:
                    sha, name = line.split("\t", 1)
                    output[name] = sha
            return output

        source_refs = refs(
            source_project["http_url_to_repo"], self.source_token, "source"
        )
        target_refs = refs(
            target_project["http_url_to_repo"], self.target_token, "target"
        )
        if source_refs != target_refs:
            missing = sorted(set(source_refs) - set(target_refs))
            extra = sorted(set(target_refs) - set(source_refs))
            changed = sorted(
                name
                for name in source_refs.keys() & target_refs.keys()
                if source_refs[name] != target_refs[name]
            )
            raise MigrationError(
                f"Verificación Git fallida: {len(missing)} refs ausentes, {len(extra)} extra y {len(changed)} SHA distintos"
            )
        target_detail = self.target.get_project(target_project["id"]) or target_project
        source_format = source_project.get("repository_object_format", "sha1")
        target_format = target_detail.get("repository_object_format", "sha1")
        if source_format != target_format:
            raise MigrationError(
                f"Formato de objetos incompatible: {source_format} -> {target_format}"
            )
        if bool(source_project.get("lfs_enabled", True)) != bool(
            target_detail.get("lfs_enabled", True)
        ):
            raise MigrationError("La configuración lfs_enabled no coincide en destino")
        default_branch = source_project.get("default_branch")
        if default_branch and f"refs/heads/{default_branch}" not in target_refs:
            raise MigrationError(
                f"La rama predeterminada {default_branch} no existe en destino"
            )
        if source_project.get("lfs_enabled") and self.args.with_lfs:
            message = (
                "Los OID de LFS requieren una verificación de muestra independiente"
            )
            if message not in result.warnings:
                result.warnings.append(message)

    def read_ci_config(
        self,
        repo_dir: Path,
        source_project: dict[str, Any],
        result: ItemResult,
    ) -> str | None:
        """Extrae el YAML de CI de la rama principal para validarlo en destino.

        Avisa de configuraciones externas, referencias al host antiguo e includes que
        pueden requerir adaptar rutas o permisos."""
        default_branch = source_project.get("default_branch")
        if not default_branch:
            return None
        config_path = source_project.get("ci_config_path") or ".gitlab-ci.yml"
        if "@" in config_path or "://" in config_path:
            result.warnings.append(
                f"La configuración CI externa requiere revisión manual: {config_path}"
            )
            return None
        completed = self.run_git(
            [
                "git",
                "show",
                f"refs/heads/{default_branch}:{config_path.lstrip('/')}",
            ],
            cwd=repo_dir,
            token=self.source_token,
            role="source",
            allow_failure=True,
        )
        if completed.returncode:
            result.warnings.append(
                f"No se encontró la configuración CI en {default_branch}:{config_path}"
            )
            return None
        content = completed.stdout
        source_host = urlsplit(clean_base_url(self.args.source_url)).netloc
        if source_host and source_host in content:
            result.warnings.append(
                "La configuración CI contiene referencias al host GitLab origen"
            )
        if "include:" in content:
            result.warnings.append(
                "La configuración CI usa include; conviene verificar rutas y permisos "
                "entre proyectos en destino"
            )
        return content

    def copy_project_variables(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> None:
        """Copia variables CI/CD del proyecto y registra su balance."""
        variables = self.source.paginate(f"/projects/{source_project['id']}/variables")
        if not variables:
            return
        copied, skipped, failed = self.copy_variables(
            variables,
            f"/projects/{target_project['id']}/variables",
            result,
        )
        result.details.append(
            f"Variables CI/CD: {copied} copiadas, {skipped} omitidas, {failed} fallidas"
        )
        if failed:
            self.mark_partial(result, "Hay variables CI/CD que no se pudieron copiar")

    def copy_project_ci_settings(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> None:
        """Replica por separado cada ajuste CI/CD compatible.

        Así una propiedad no soportada no bloquea las demás y los recursos que requieren
        credenciales nuevas quedan señalados para revisión."""
        full_source = self.source.get_project(source_project["id"]) or source_project
        allowed_fields = (
            "build_timeout",
            "auto_cancel_pending_pipelines",
            "ci_config_path",
            "public_builds",
            "shared_runners_enabled",
            "group_runners_enabled",
            "auto_devops_enabled",
            "auto_devops_deploy_strategy",
            "ci_pipeline_variables_minimum_override_role",
            "merge_pipelines_enabled",
            "merge_trains_enabled",
        )
        payload = {
            field: full_source[field]
            for field in allowed_fields
            if field in full_source and full_source[field] is not None
        }
        if payload:
            copied_settings = 0
            for field_name, value in payload.items():
                try:
                    self.target.request(
                        "PUT",
                        f"/projects/{target_project['id']}",
                        data={field_name: value},
                    )
                    copied_settings += 1
                except MigrationError as exc:
                    self.mark_partial(
                        result,
                        f"Ajuste CI/CD obligatorio no copiado ({field_name}): {redact(str(exc), self.secrets)}",
                    )
            result.details.append(
                f"Ajustes CI/CD compatibles copiados: {copied_settings}/{len(payload)}"
            )
        result.warnings.append(
            "Los runners y sus tokens no se migran; hay que comprobar disponibilidad y tags"
        )
        result.warnings.append(
            "Los triggers, webhooks, Container Registry, paquetes y secure files "
            "requieren migración separada"
        )

    def copy_deploy_keys(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> None:
        """Copia claves SSH públicas de despliegue de forma idempotente."""
        target_endpoint = f"/projects/{target_project['id']}/deploy_keys"
        try:
            source_keys = self.source.paginate(
                f"/projects/{source_project['id']}/deploy_keys"
            )
            target_by_key = {
                item.get("key"): item
                for item in self.target.paginate(target_endpoint)
                if item.get("key")
            }
        except MigrationError as exc:
            self.mark_partial(
                result,
                "No se pudieron inventariar deploy keys: "
                + redact(str(exc), self.secrets),
            )
            return
        copied = 0
        for source_key in source_keys:
            public_key = source_key.get("key")
            if not public_key:
                self.mark_partial(result, "Deploy key sin material público recuperable")
                continue
            existing = target_by_key.get(public_key)
            try:
                if existing:
                    if bool(existing.get("can_push")) != bool(
                        source_key.get("can_push")
                    ):
                        self.target.request(
                            "PUT",
                            f"{target_endpoint}/{existing['id']}",
                            data={"can_push": bool(source_key.get("can_push"))},
                        )
                    continue
                payload: dict[str, Any] = {
                    "title": source_key.get("title") or "Deploy key migrada",
                    "key": public_key,
                    "can_push": bool(source_key.get("can_push")),
                }
                if source_key.get("expires_at"):
                    payload["expires_at"] = source_key["expires_at"]
                created = self.target.request("POST", target_endpoint, data=payload)
                target_by_key[public_key] = created
                copied += 1
            except MigrationError as exc:
                self.mark_partial(
                    result,
                    f"No se pudo copiar deploy key {source_key.get('title', '?')}: "
                    f"{redact(str(exc), self.secrets)}",
                )
        if source_keys:
            result.details.append(
                f"Deploy keys: {copied} creadas, {len(source_keys) - copied} existentes o parciales"
            )

    def resolve_environment_identity(
        self, entry: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Mapea roles, usuarios y grupos de una regla de entorno protegido."""
        mapped: dict[str, Any]
        if entry.get("user_id"):
            source_user = self.source.request("GET", f"/users/{entry['user_id']}")
            users = self.target.paginate(
                "/users", query={"username": source_user["username"]}
            )
            exact = next(
                (
                    item
                    for item in users
                    if item.get("username") == source_user["username"]
                ),
                None,
            )
            if not exact:
                return None
            mapped = {"user_id": exact["id"]}
        elif entry.get("group_id"):
            source_group = self.source.get_group(entry["group_id"])
            if not source_group:
                return None
            source_path = source_group["full_path"]
            if source_path == self.source_root["full_path"] or source_path.startswith(
                self.source_root["full_path"] + "/"
            ):
                source_path = self.target_group_path(source_path)
            target_group = self.target.get_group(source_path)
            if not target_group:
                return None
            mapped = {"group_id": target_group["id"]}
        elif entry.get("access_level") is not None:
            mapped = {"access_level": int(entry["access_level"])}
        else:
            return None
        for field_name in ("group_inheritance_type", "required_approvals"):
            if entry.get(field_name) is not None:
                mapped[field_name] = entry[field_name]
        return mapped

    @staticmethod
    def normalize_environment_entries(
        entries: list[dict[str, Any]],
    ) -> set[tuple[Any, ...]]:
        """Normaliza ACL de entornos para comparar sin IDs internos ni orden."""
        normalized: set[tuple[Any, ...]] = set()
        for entry in entries or []:
            identity = next(
                (
                    (key, entry[key])
                    for key in ("user_id", "group_id", "access_level")
                    if entry.get(key) is not None
                ),
                ("unknown", None),
            )
            normalized.add(
                identity
                + (
                    entry.get("group_inheritance_type", 0),
                    entry.get("required_approvals"),
                )
            )
        return normalized

    def copy_protected_environments(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> None:
        """Copia entornos protegidos sin reemplazar una ACL destino distinta."""
        source_endpoint = f"/projects/{source_project['id']}/protected_environments"
        target_endpoint = f"/projects/{target_project['id']}/protected_environments"
        try:
            source_environments = self.source.paginate(source_endpoint)
            target_environments = {
                item["name"]: item for item in self.target.paginate(target_endpoint)
            }
        except MigrationError as exc:
            self.mark_partial(
                result,
                "No se pudieron inventariar entornos protegidos: "
                + redact(str(exc), self.secrets),
            )
            return
        copied = 0
        for environment in source_environments:
            payload: dict[str, Any] = {"name": environment["name"]}
            unresolved: list[str] = []
            for source_field, target_field in (
                ("deploy_access_levels", "deploy_access_levels"),
                ("approval_rules", "approval_rules"),
            ):
                mapped_entries: list[dict[str, Any]] = []
                for entry in environment.get(source_field, []):
                    try:
                        mapped = self.resolve_environment_identity(entry)
                    except MigrationError as exc:
                        unresolved.append(redact(str(exc), self.secrets))
                        continue
                    if mapped is None:
                        unresolved.append(
                            f"{source_field}:{entry.get('user_id') or entry.get('group_id') or entry.get('access_level') or '?'}"
                        )
                    else:
                        mapped_entries.append(mapped)
                if source_field == "deploy_access_levels" or mapped_entries:
                    payload[target_field] = mapped_entries
            if environment.get(
                "required_approval_count"
            ) is not None and not payload.get("approval_rules"):
                payload["required_approval_count"] = environment[
                    "required_approval_count"
                ]
            if unresolved:
                self.mark_partial(
                    result,
                    f"Entorno {environment['name']} no aplicado; identidades sin mapear: "
                    + ", ".join(unresolved),
                )
                continue
            existing = target_environments.get(environment["name"])
            if existing:
                mismatches = []
                if self.normalize_environment_entries(
                    existing.get("deploy_access_levels", [])
                ) != self.normalize_environment_entries(
                    payload.get("deploy_access_levels", [])
                ):
                    mismatches.append("deploy_access_levels")
                if self.normalize_environment_entries(
                    existing.get("approval_rules", [])
                ) != self.normalize_environment_entries(
                    payload.get("approval_rules", [])
                ):
                    mismatches.append("approval_rules")
                if (
                    "required_approval_count" in payload
                    and existing.get("required_approval_count")
                    != payload["required_approval_count"]
                ):
                    mismatches.append("required_approval_count")
                if mismatches:
                    self.mark_partial(
                        result,
                        f"Entorno protegido existente {environment['name']} difiere ({', '.join(mismatches)}); no se relaja",
                    )
                continue
            try:
                self.target.request("POST", target_endpoint, data=payload)
                copied += 1
            except MigrationError as exc:
                self.mark_partial(
                    result,
                    f"No se pudo proteger el entorno {environment['name']}: {redact(str(exc), self.secrets)}",
                )
        if source_environments:
            result.details.append(
                f"Entornos protegidos creados: {copied}/{len(source_environments)}"
            )

    def map_allowlisted_project(
        self, source_entry: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Resuelve un proyecto de allowlist solo cuando pertenece al ámbito migrado."""
        source_path = source_entry.get("path_with_namespace")
        root = self.source_root["full_path"]
        if not source_path or not (
            source_path == root or source_path.startswith(root + "/")
        ):
            return None
        namespace, project_path = source_path.rsplit("/", 1)
        return self.target.get_project(
            f"{self.target_group_path(namespace)}/{project_path}"
        )

    def map_allowlisted_group(
        self, source_entry: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Resuelve un grupo de allowlist por su ruta dentro del ámbito migrado."""
        source_group = self.source.get_group(source_entry["id"])
        if not source_group:
            return None
        source_path = source_group["full_path"]
        root = self.source_root["full_path"]
        if not (source_path == root or source_path.startswith(root + "/")):
            return None
        return self.target.get_group(self.target_group_path(source_path))

    def copy_job_token_scope(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> None:
        """Copia el modo y las allowlists de CI_JOB_TOKEN sin borrar extras destino."""
        source_base = f"/projects/{source_project['id']}/job_token_scope"
        target_base = f"/projects/{target_project['id']}/job_token_scope"
        try:
            source_scope = self.source.request("GET", source_base)
            source_projects = self.source.paginate(f"{source_base}/allowlist")
            source_groups = self.source.paginate(f"{source_base}/groups_allowlist")
            target_projects = self.target.paginate(f"{target_base}/allowlist")
            target_groups = self.target.paginate(f"{target_base}/groups_allowlist")
        except MigrationError as exc:
            self.mark_partial(
                result,
                "No se pudo inventariar CI_JOB_TOKEN scope: "
                + redact(str(exc), self.secrets),
            )
            return

        desired_project_ids: set[int] = set()
        for entry in source_projects:
            mapped = self.map_allowlisted_project(entry)
            if not mapped:
                self.mark_partial(
                    result,
                    f"Proyecto externo/no resuelto en job-token allowlist: {entry.get('path_with_namespace') or entry.get('id')}",
                )
                continue
            desired_project_ids.add(int(mapped["id"]))
        desired_group_ids: set[int] = set()
        for entry in source_groups:
            mapped = self.map_allowlisted_group(entry)
            if not mapped:
                self.mark_partial(
                    result,
                    f"Grupo externo/no resuelto en job-token allowlist: {entry.get('name') or entry.get('id')}",
                )
                continue
            desired_group_ids.add(int(mapped["id"]))

        current_project_ids = {int(item["id"]) for item in target_projects}
        current_group_ids = {int(item["id"]) for item in target_groups}
        for project_id in sorted(desired_project_ids - current_project_ids):
            try:
                self.target.request(
                    "POST",
                    f"{target_base}/allowlist",
                    data={"target_project_id": project_id},
                )
            except MigrationError as exc:
                self.mark_partial(
                    result,
                    f"No se añadió proyecto {project_id} a job-token allowlist: {redact(str(exc), self.secrets)}",
                )
        for group_id in sorted(desired_group_ids - current_group_ids):
            try:
                self.target.request(
                    "POST",
                    f"{target_base}/groups_allowlist",
                    data={"target_group_id": group_id},
                )
            except MigrationError as exc:
                self.mark_partial(
                    result,
                    f"No se añadió grupo {group_id} a job-token allowlist: {redact(str(exc), self.secrets)}",
                )

        enabled = bool(source_scope.get("inbound_enabled"))
        if enabled:
            extra_projects = current_project_ids - desired_project_ids
            extra_groups = current_group_ids - desired_group_ids
            if extra_projects or extra_groups:
                self.mark_partial(
                    result,
                    f"Job-token allowlist conserva extras destino: {len(extra_projects)} proyectos y {len(extra_groups)} grupos",
                )
        try:
            self.target.request(
                "PATCH", target_base, data={"enabled": enabled}, expect_json=False
            )
        except MigrationError as exc:
            self.mark_partial(
                result,
                "No se pudo ajustar CI_JOB_TOKEN scope: "
                + redact(str(exc), self.secrets),
            )
            return
        result.details.append(
            f"CI_JOB_TOKEN scope reconciliado: {len(desired_project_ids)} proyectos y {len(desired_group_ids)} grupos"
        )

    def copy_protected_refs(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> None:
        """Copia reglas compatibles de ramas y etiquetas protegidas."""
        branch_count = self.copy_protected_ref_type(
            source_project,
            target_project,
            kind="branches",
            result=result,
        )
        tag_count = self.copy_protected_ref_type(
            source_project,
            target_project,
            kind="tags",
            result=result,
        )
        if branch_count or tag_count:
            result.details.append(
                f"Protecciones copiadas: {branch_count} ramas, {tag_count} etiquetas"
            )

    def resolve_protection_identity(
        self,
        entry: dict[str, Any],
        source_project: dict[str, Any],
        target_project: dict[str, Any],
    ) -> dict[str, int] | None:
        """Mapea roles, usuarios, grupos y deploy keys por identidades estables."""
        if entry.get("access_level") is not None and not any(
            entry.get(key) for key in ("user_id", "group_id", "deploy_key_id")
        ):
            return {"access_level": int(entry["access_level"])}
        if entry.get("user_id"):
            source_user = self.source.request("GET", f"/users/{entry['user_id']}")
            candidates = self.target.paginate(
                "/users", query={"username": source_user["username"]}
            )
            exact = next(
                (
                    user
                    for user in candidates
                    if user.get("username") == source_user["username"]
                ),
                None,
            )
            return {"user_id": exact["id"]} if exact else None
        if entry.get("group_id"):
            source_group = self.source.get_group(entry["group_id"])
            if not source_group:
                return None
            path = source_group["full_path"]
            if path == self.source_root["full_path"] or path.startswith(
                self.source_root["full_path"] + "/"
            ):
                path = self.target_group_path(path)
            target_group = self.target.get_group(path)
            return {"group_id": target_group["id"]} if target_group else None
        if entry.get("deploy_key_id"):
            source_keys = self.source.paginate(
                f"/projects/{source_project['id']}/deploy_keys"
            )
            target_keys = self.target.paginate(
                f"/projects/{target_project['id']}/deploy_keys"
            )
            source_key = next(
                (key for key in source_keys if key.get("id") == entry["deploy_key_id"]),
                None,
            )
            if not source_key:
                return None
            fingerprints = ("fingerprint_sha256", "fingerprint", "title")
            match = next(
                (
                    candidate
                    for candidate in target_keys
                    if any(
                        source_key.get(field)
                        and source_key.get(field) == candidate.get(field)
                        for field in fingerprints
                    )
                ),
                None,
            )
            return {"deploy_key_id": match["id"]} if match else None
        return None

    @staticmethod
    def normalize_access_entries(entries: list[dict[str, Any]]) -> set[tuple[str, int]]:
        """Normaliza una lista de accesos para comparar reglas sin depender del orden."""
        normalized: set[tuple[str, int]] = set()
        for entry in entries or []:
            for key in ("user_id", "group_id", "deploy_key_id", "access_level"):
                if entry.get(key) is not None:
                    normalized.add((key, int(entry[key])))
                    break
        return normalized

    def copy_protected_ref_type(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        *,
        kind: str,
        result: ItemResult,
    ) -> int:
        """Crea protecciones exactas y nunca degrada silenciosamente una ya existente."""
        source_endpoint = f"/projects/{source_project['id']}/protected_{kind}"
        target_endpoint = f"/projects/{target_project['id']}/protected_{kind}"
        source_refs = self.source.paginate(source_endpoint)
        target_refs = {
            item["name"]: item for item in self.target.paginate(target_endpoint)
        }
        copied = 0
        mappings = (
            (
                ("push_access_levels", "allowed_to_push"),
                ("merge_access_levels", "allowed_to_merge"),
                ("unprotect_access_levels", "allowed_to_unprotect"),
            )
            if kind == "branches"
            else (("create_access_levels", "allowed_to_create"),)
        )
        for protected_ref in source_refs:
            name = protected_ref["name"]
            payload: dict[str, Any] = {"name": name}
            unresolved: list[str] = []
            for source_field, target_field in mappings:
                mapped: list[dict[str, int]] = []
                for entry in protected_ref.get(source_field, []):
                    try:
                        resolved = self.resolve_protection_identity(
                            entry, source_project, target_project
                        )
                    except MigrationError as exc:
                        unresolved.append("API:" + redact(str(exc), self.secrets))
                        continue
                    if resolved is None:
                        unresolved.append(
                            f"{source_field}:{entry.get('user_id') or entry.get('group_id') or entry.get('deploy_key_id') or '?'}"
                        )
                    else:
                        mapped.append(resolved)
                payload[target_field] = mapped
            if kind == "branches":
                for field_name in ("allow_force_push", "code_owner_approval_required"):
                    if field_name in protected_ref:
                        payload[field_name] = protected_ref[field_name]
            if unresolved:
                self.mark_partial(
                    result,
                    f"Protección {name} no aplicada: identidades sin mapear ({', '.join(unresolved)})",
                )
                continue
            existing = target_refs.get(name)
            if existing:
                mismatches = []
                for source_field, target_field in mappings:
                    if self.normalize_access_entries(
                        existing.get(source_field, [])
                    ) != self.normalize_access_entries(payload[target_field]):
                        mismatches.append(source_field)
                for field_name in (
                    ("allow_force_push", "code_owner_approval_required")
                    if kind == "branches"
                    else ()
                ):
                    if existing.get(field_name, False) != payload.get(
                        field_name, False
                    ):
                        mismatches.append(field_name)
                if mismatches:
                    self.mark_partial(
                        result,
                        f"Protección existente {name} difiere ({', '.join(mismatches)}); no se elimina ni relaja automáticamente",
                    )
                continue
            try:
                self.target.request("POST", target_endpoint, data=payload)
                copied += 1
            except MigrationError as exc:
                self.mark_partial(
                    result,
                    f"No se pudo proteger {name}: {redact(str(exc), self.secrets)}",
                )
        return copied

    def copy_pipeline_schedules(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> None:
        """Crea o reconcilia schedules, inputs y variables, siempre inactivas."""
        source_endpoint = f"/projects/{source_project['id']}/pipeline_schedules"
        target_endpoint = f"/projects/{target_project['id']}/pipeline_schedules"
        schedules = self.source.paginate(source_endpoint)
        existing = self.target.paginate(target_endpoint)
        existing_by_identity = {
            (item.get("description"), item.get("ref"), item.get("cron")): item
            for item in existing
        }
        reconciled = 0
        for schedule in schedules:
            identity = (
                schedule.get("description"),
                schedule.get("ref"),
                schedule.get("cron"),
            )
            detail = self.source.request("GET", f"{source_endpoint}/{schedule['id']}")
            candidate = existing_by_identity.get(identity)
            schedule_payload: dict[str, Any] = {
                "description": schedule["description"],
                "ref": schedule["ref"],
                "cron": schedule["cron"],
                "cron_timezone": schedule.get("cron_timezone", "UTC"),
                "active": False,
            }
            if detail.get("inputs") is not None:
                schedule_payload["inputs"] = detail["inputs"]

            try:
                if candidate:
                    created = candidate
                    update_payload = {
                        "active": False,
                        "cron_timezone": schedule_payload["cron_timezone"],
                    }
                    if "inputs" in schedule_payload:
                        update_payload["inputs"] = schedule_payload["inputs"]
                    self.target.request(
                        "PUT",
                        f"{target_endpoint}/{candidate['id']}",
                        data=update_payload,
                    )
                else:
                    created = self.target.request(
                        "POST", target_endpoint, data=schedule_payload
                    )
                    existing_by_identity[identity] = created
            except MigrationError as exc:
                self.mark_partial(
                    result,
                    f"No se pudo crear o reconciliar la schedule {schedule.get('description')}: "
                    f"{redact(str(exc), self.secrets)}",
                )
                continue

            target_detail = self.target.request(
                "GET", f"{target_endpoint}/{created['id']}"
            )
            if target_detail.get("active"):
                self.mark_partial(
                    result,
                    f"La schedule no quedó inactiva: {schedule.get('description')}",
                )
                continue
            if (detail.get("inputs") or []) != (
                target_detail.get("inputs") or []
            ) or schedule.get("cron_timezone", "UTC") != target_detail.get(
                "cron_timezone", "UTC"
            ):
                self.mark_partial(
                    result,
                    f"Schedule no reconciliada en zona horaria o inputs: {schedule.get('description')}",
                )

            source_variables = {
                item.get("key"): item
                for item in detail.get("variables") or []
                if item.get("key")
            }
            target_variables = {
                item.get("key"): item
                for item in target_detail.get("variables") or []
                if item.get("key")
            }
            for variable_key, variable in source_variables.items():
                variable_payload = {
                    field: variable[field]
                    for field in ("key", "value", "variable_type")
                    if field in variable
                }
                try:
                    current = target_variables.get(variable_key)
                    if current is None:
                        self.target.request(
                            "POST",
                            f"{target_endpoint}/{created['id']}/variables",
                            data=variable_payload,
                        )
                    elif current.get("value") != variable.get("value") or current.get(
                        "variable_type", "env_var"
                    ) != variable.get("variable_type", "env_var"):
                        self.target.request(
                            "PUT",
                            f"{target_endpoint}/{created['id']}/variables/{encode_path(variable['key'])}",
                            data=variable_payload,
                        )
                except MigrationError as exc:
                    self.mark_partial(
                        result,
                        f"Variable de schedule {schedule['description']}/{variable.get('key')}: "
                        f"{redact(str(exc), self.secrets + [str(variable.get('value', ''))])}",
                    )
            extra_variables = sorted(set(target_variables) - set(source_variables))
            if extra_variables:
                self.mark_partial(
                    result,
                    f"Schedule {schedule['description']} conserva variables exclusivas de destino: "
                    + ", ".join(key or "?" for key in extra_variables),
                )
            reconciled += 1
        if reconciled:
            result.details.append(f"{reconciled} schedules reconciliadas e inactivas")

    def restore_project_state(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> None:
        """Restaura rama predeterminada y acceso CI/CD sin ejecutar pipelines.

        Las schedules permanecen inactivas aunque se rehabilite el acceso a builds."""
        full_source = self.source.get_project(source_project["id"]) or source_project
        payload: dict[str, Any] = {
            "builds_access_level": full_source.get("builds_access_level", "enabled"),
        }
        if source_project.get("default_branch"):
            payload["default_branch"] = source_project["default_branch"]
        self.target.request(
            "PUT",
            f"/projects/{target_project['id']}",
            data=payload,
        )
        refreshed = self.target.get_project(target_project["id"]) or {}
        expected_branch = payload.get("default_branch")
        if expected_branch and refreshed.get("default_branch") != expected_branch:
            raise MigrationError(
                f"No se restauró la rama predeterminada: {refreshed.get('default_branch')} != {expected_branch}"
            )
        if refreshed.get("builds_access_level") != payload["builds_access_level"]:
            raise MigrationError("No se restauró el nivel de acceso de CI/CD")
        result.details.append(
            "Rama predeterminada y acceso CI/CD restaurados y verificados"
        )

    def validate_ci(self, target_project: dict[str, Any], result: ItemResult) -> None:
        """Valida la configuración almacenada tras migrar todos los includes y revisa runners."""
        detail = self.target.get_project(target_project["id"]) or target_project
        default_branch = detail.get("default_branch")
        config_path = detail.get("ci_config_path") or ".gitlab-ci.yml"
        if not default_branch:
            result.details.append(
                "CI Lint omitido: repositorio sin rama predeterminada"
            )
            if result.status == "ok":
                self.checkpoint_phase(result.source, "complete", target_project)
            return
        if "@" in config_path or "://" in config_path:
            self.mark_partial(
                result, f"CI externa requiere validación manual: {config_path}"
            )
            return
        encoded_file = encode_path(config_path.lstrip("/"))
        file_data = self.target.request(
            "GET",
            f"/projects/{target_project['id']}/repository/files/{encoded_file}",
            query={"ref": default_branch},
            allow_not_found=True,
        )
        if not file_data:
            result.details.append(f"CI Lint omitido: no existe {config_path}")
            if result.status == "ok":
                self.checkpoint_phase(result.source, "complete", target_project)
            return
        try:
            content = base64.b64decode(file_data["content"]).decode(
                "utf-8", errors="replace"
            )
            lint = self.target.request(
                "POST",
                f"/projects/{target_project['id']}/ci/lint",
                data={
                    "content": content,
                    "dry_run": True,
                    "include_jobs": True,
                    "ref": default_branch,
                },
            )
        except (MigrationError, ValueError, KeyError) as exc:
            self.mark_partial(
                result,
                "No se pudo ejecutar CI Lint efectivo: "
                + redact(str(exc), self.secrets),
            )
            return
        valid = lint.get("valid")
        if valid is None:
            valid = lint.get("status") == "valid"
        if not valid:
            errors = lint.get("errors") or [lint.get("message", "Error no detallado")]
            self.mark_partial(
                result, "CI Lint no válido: " + "; ".join(map(str, errors))
            )
            return
        result.details.append("CI Lint válido con dry_run e includes resueltos")
        jobs = lint.get("jobs") or []
        runners = self.target.paginate(
            f"/projects/{target_project['id']}/runners", query={"status": "online"}
        )
        available_runners = [
            runner for runner in runners if not runner.get("paused", False)
        ]
        unavailable: list[str] = []
        for job in jobs:
            tags = set(job.get("tag_list") or job.get("tags") or [])
            if tags:
                compatible = any(
                    tags.issubset(set(runner.get("tag_list") or []))
                    for runner in available_runners
                )
                label = ",".join(sorted(tags))
            else:
                compatible = any(
                    runner.get("run_untagged", False) for runner in available_runners
                )
                label = "sin-tags"
            if not compatible:
                unavailable.append(f"{job.get('name', '?')}[{label}]")
        if unavailable:
            self.mark_partial(
                result, "Jobs sin runner online compatible: " + ", ".join(unavailable)
            )
        else:
            result.details.append(f"Runners revisados para {len(jobs)} jobs expandidos")
            if result.status == "ok":
                self.checkpoint_phase(result.source, "complete", target_project)

    def copy_wiki(
        self,
        source_project: dict[str, Any],
        target_project: dict[str, Any],
        result: ItemResult,
    ) -> None:
        """Intenta copiar el repositorio Git independiente de la wiki.

        Una wiki ausente es normal y un fallo de envío no invalida el repositorio principal."""
        source_url = (
            source_project["http_url_to_repo"].removesuffix(".git") + ".wiki.git"
        )
        target_url = (
            target_project["http_url_to_repo"].removesuffix(".git") + ".wiki.git"
        )
        with tempfile.TemporaryDirectory(prefix="glim-wiki-") as temp_dir:
            wiki_dir = Path(temp_dir) / "wiki.git"
            clone = self.run_git(
                ["git", "clone", "--mirror", source_url, str(wiki_dir)],
                cwd=None,
                token=self.source_token,
                role="source",
                allow_failure=True,
            )
            if clone.returncode:
                LOG.debug(
                    "Wiki no disponible para %s", source_project["path_with_namespace"]
                )
                return
            self.run_git(
                ["git", "remote", "add", "destination", target_url],
                cwd=wiki_dir,
                token=self.target_token,
                role="target",
            )
            push = self.run_git(
                ["git", "push", "--mirror", "destination"],
                cwd=wiki_dir,
                token=self.target_token,
                role="target",
                allow_failure=True,
            )
            if push.returncode:
                result.warnings.append(
                    "No se pudo migrar la wiki: "
                    + redact(push.stderr.strip()[-1000:], self.secrets)
                )
            else:
                result.details.append("Wiki migrada")


def default_report_path() -> Path:
    """Genera una ruta de informe única con fecha y hora locales."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(f"migration-report-{stamp}.json")


def write_report(report: MigrationReport, path: Path) -> None:
    """Escribe el informe JSON en UTF-8 y crea sus directorios padre."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def configure_logging(verbose: bool) -> None:
    """Configura logs informativos o detallados con formato uniforme."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )


def main() -> int:
    """Valida argumentos y secretos, ejecuta la migración y escribe el informe.

    Devuelve 0 sin fallos, 1 ante error global, 2 con proyectos fallidos y 130 ante
    interrupción del usuario."""
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.verbose)

    # Los secretos solo se aceptan por entorno, no mediante argumentos visibles.
    source_token = os.environ.get(SOURCE_TOKEN_ENV)
    target_token = os.environ.get(TARGET_TOKEN_ENV)
    missing = [
        name
        for name, value in (
            (SOURCE_TOKEN_ENV, source_token),
            (TARGET_TOKEN_ENV, target_token),
        )
        if not value
    ]
    if missing:
        parser.error("Faltan variables de entorno obligatorias: " + ", ".join(missing))

    report_path = args.report or default_report_path()
    migrator: Migrator | None = None
    try:
        migrator = Migrator(args, source_token, target_token)
        report = migrator.run()
    except KeyboardInterrupt:
        LOG.error("Migración interrumpida por el usuario")
        return 130
    except (MigrationError, OSError, ssl.SSLError) as exc:
        message = redact(str(exc), [source_token, target_token])
        if migrator is None:
            failed_report = MigrationReport(
                mode="execute" if args.execute else "dry-run",
                source_url=clean_base_url(args.source_url),
                target_url=clean_base_url(args.target_url),
                source_group=args.source_group,
                target_group=args.target_group,
            )
        else:
            failed_report = migrator.report
        failed_report.errors.append(message)
        failed_report.finished_at = utc_now()
        write_report(failed_report, report_path)
        LOG.error("%s", message)
        LOG.info("Informe escrito en %s", report_path.resolve())
        return 1

    write_report(report, report_path)
    summary = report.as_dict()["summary"]
    LOG.info(
        "Finalizado: %s correctos, %s parciales, %s planificados, %s omitidos, %s fallidos",
        summary["ok"],
        summary["partial"],
        summary["planned"],
        summary["skipped"],
        summary["failed"],
    )
    LOG.info("Informe escrito en %s", report_path.resolve())
    return 2 if summary["failed"] or summary["partial"] else 0


if __name__ == "__main__":
    sys.exit(main())
