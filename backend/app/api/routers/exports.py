from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_storage
from app.api.schemas import ExportArtifactOut, ExportCreateIn
from app.application import export_service
from app.core.security import Principal, UserRole, require_roles
from app.infrastructure.db.session import get_db
from app.infrastructure.storage.local import LocalStorage

router = APIRouter(tags=["exports"])


def _to_out(artifact) -> ExportArtifactOut:
    return ExportArtifactOut(
        id=artifact.id,
        novel_id=artifact.novel_id,
        format=artifact.format,
        status=artifact.status,
        data_revision=artifact.data_revision,
        file_size_bytes=artifact.file_size_bytes,
        download_url=f"/api/v1/exports/{artifact.id}/download",
        created_at=artifact.created_at,
    )


@router.post(
    "/novels/{novel_id}/exports",
    response_model=ExportArtifactOut,
    status_code=status.HTTP_201_CREATED,
)
def create_export(
    novel_id: str,
    body: ExportCreateIn,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    artifact = export_service.create_export(db, storage, novel_id=novel_id, fmt=body.format)
    return _to_out(artifact)


@router.get("/novels/{novel_id}/exports", response_model=list[ExportArtifactOut])
def list_exports(
    novel_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return [_to_out(a) for a in export_service.list_exports(db, novel_id)]


@router.get("/exports/{export_id}", response_model=ExportArtifactOut)
def get_export(
    export_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return _to_out(export_service.get_export(db, export_id))


@router.get("/exports/{export_id}/download")
def download_export(
    export_id: str,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    artifact = export_service.get_export(db, export_id)
    file_path = storage.get_export_path(artifact.storage_ref)
    media_types = {
        "json": "application/json",
        "csv": "text/csv; charset=utf-8",
        "graphml": "application/xml",
    }
    return FileResponse(
        path=file_path,
        media_type=media_types.get(artifact.format, "application/octet-stream"),
        filename=f"ncke-{artifact.novel_id[:8]}-rev{artifact.data_revision}.{artifact.format}",
    )
