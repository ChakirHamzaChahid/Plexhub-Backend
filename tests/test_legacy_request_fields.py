"""Lot 10 C: champs retirés des schémas de requête (ignorés par le serveur).

Les anciens clients qui envoient encore ces champs ne doivent pas prendre de
422 : Pydantic ignore les clés inconnues (pas de ``extra="forbid"``).
"""
from app.api.ai import SubtitleTranslateRequest
from app.api.plex import GenerateRequest
from app.models.schemas import SyncRequest


def test_sync_request_ignores_legacy_force():
    req = SyncRequest.model_validate({"accountId": "a1", "force": True})
    assert req.account_id == "a1"
    assert not hasattr(req, "force")


def test_subtitle_translate_request_ignores_legacy_ids():
    req = SubtitleTranslateRequest.model_validate(
        {"content": "x", "mediaId": "m", "ratingKey": "r"}
    )
    assert req.content == "x"
    assert not hasattr(req, "media_id") and not hasattr(req, "rating_key")


def test_generate_request_ignores_legacy_all_accounts():
    req = GenerateRequest.model_validate({"allAccounts": True})
    assert req.account_id is None
    assert not hasattr(req, "all_accounts")
