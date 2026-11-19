from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_storage
from app.api.schemas import (
    VoiceDesignIn,
    VoiceLockIn,
    VoiceLockResultOut,
    VoiceProfileCreateIn,
    VoiceProfileOut,
    VoiceProfileUpdateIn,
)
from app.application import voice_service
from app.core.config import Settings, get_settings
from app.core.security import Principal, UserRole, require_roles
from app.infrastructure.db.session import get_db
from app.infrastructure.storage.local import LocalStorage

router = APIRouter(tags=["voices"])


@router.get("/novels/{novel_id}/voice-profiles", response_model=list[VoiceProfileOut])
def list_voice_profiles(
    novel_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return voice_service.list_voice_profiles(db, novel_id)


@router.post(
    "/novels/{novel_id}/voice-profiles",
    response_model=VoiceProfileOut,
    status_code=status.HTTP_201_CREATED,
)
def create_voice_profile(
    novel_id: str,
    body: VoiceProfileCreateIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return voice_service.create_voice_profile(
        db,
        settings,
        novel_id=novel_id,
        character_id=body.character_id,
        narrator_key=body.narrator_key,
        display_name=body.display_name,
        voice_prompt=body.voice_prompt,
        voice_id=body.voice_id,
        language=body.language,
        default_rate=body.default_rate,
        pitch=body.pitch,
        seed=body.seed,
        consent_confirmed=body.consent_confirmed,
        consent_note=body.consent_note,
    )


@router.post("/voice-profiles/{profile_id}/design-candidates", response_model=VoiceProfileOut)
def design_voice_candidates(
    profile_id: str,
    body: VoiceDesignIn,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return voice_service.design_voice_candidates(
        db, storage, settings, profile_id=profile_id, preview_text=body.preview_text
    )


@router.post("/voice-profiles/{profile_id}/lock", response_model=VoiceLockResultOut)
def lock_voice_profile(
    profile_id: str,
    body: VoiceLockIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return voice_service.lock_voice_profile(
        db,
        settings,
        profile_id=profile_id,
        candidate_index=body.candidate_index,
        voice_id=body.voice_id,
        voice_prompt=body.voice_prompt,
        scope=body.scope,
    )


@router.patch("/voice-profiles/{profile_id}", response_model=VoiceProfileOut)
def update_voice_profile(
    profile_id: str,
    body: VoiceProfileUpdateIn,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return voice_service.update_voice_profile(
        db,
        profile_id=profile_id,
        display_name=body.display_name,
        voice_prompt=body.voice_prompt,
        voice_id=body.voice_id,
        default_rate=body.default_rate,
        pitch=body.pitch,
        consent_confirmed=body.consent_confirmed,
        consent_note=body.consent_note,
    )


@router.get("/voice-profiles/{profile_id}/preview")
def stream_voice_preview(
    profile_id: str,
    candidate_index: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    path = voice_service.get_preview_audio_path(db, storage, profile_id, candidate_index=candidate_index)
    media_type = "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg"
    return FileResponse(path=path, media_type=media_type, filename=path.name)
