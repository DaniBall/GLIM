# Política de seguridad

## Ámbito

Este repositorio contiene una herramienta administrativa para migrar repositorios y configuración CI/CD entre instancias de GitLab. Un fallo puede afectar a referencias Git, permisos, variables, schedules o proyectos del destino, especialmente cuando se usa `--existing update`.

## Comunicación responsable

No publiques vulnerabilidades, tokens, URLs internas, nombres de proyectos, informes de migración ni datos de la infraestructura empresarial en una issue pública.

Para comunicar un problema de seguridad:

1. Usa la función **Security advisories** del repositorio de GitHub, si está habilitada.
2. Si el repositorio pertenece a una organización, utiliza el canal privado de seguridad definido por esa organización.
3. Incluye una descripción reproducible, la versión o commit afectado, el impacto estimado y una propuesta de mitigación si la tienes.
4. Sustituye tokens, dominios, rutas internas y valores sensibles por marcadores ficticios.

Si no hay ningún canal privado disponible, contacta con un mantenedor por un medio corporativo aprobado antes de compartir detalles técnicos.

## Tratamiento de secretos

- Los tokens solo deben entregarse al proceso mediante `GITLAB_SOURCE_TOKEN` y `GITLAB_TARGET_TOKEN`.
- No añadas tokens, certificados privados, archivos `.env`, checkpoints ni informes reales al repositorio.
- Revisa y redacta los logs e informes antes de adjuntarlos a una incidencia.
- Usa credenciales temporales, con el alcance mínimo y fecha de caducidad.
- Revoca y rota inmediatamente cualquier secreto expuesto.

## Uso seguro

Ejecuta siempre primero un dry-run y prueba en namespaces aislados. Revisa el informe antes de añadir `--execute`. Trata `--existing update` como una operación potencialmente destructiva y conserva una vía de recuperación.

Los mantenedores intentarán confirmar la recepción, evaluar el impacto y coordinar una corrección antes de publicar información sobre la vulnerabilidad. Los plazos dependerán de la gravedad y de la disponibilidad del equipo.
