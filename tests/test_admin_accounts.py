"""tests/test_admin_accounts.py — Tests pour la gestion des comptes Xtream et l'éditeur de catégories (/admin/accounts).

Couvre les critères du ticket T9.1 :
- Auth Basic requise (401 sans auth) et CSRF Sec-Fetch-Site: same-origin requis sur POST (403 si cross-site)
- Absence absolue de mot de passe dans le HTML (clair ou chiffré)
- Ajout de compte Xtream (mock respx) avec gestion des erreurs 409 et 422
- Test de connexion (mock respx)
- Toggle actif/inactif
- Éditeur de catégories : listing, mode de filtrage, toggle unitaire, filtre adulte
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import database as db_module
from app.models.database import XtreamAccount, XtreamCategory

ADMIN_USER = "admin"
ADMIN_PASS = "test-admin-accounts-pass"
ADMIN_AUTH = (ADMIN_USER, ADMIN_PASS)
CSRF_HEADERS = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture(autouse=True)
def _configure_environment(monkeypatch, db_factory):
    from app.api import admin as admin_module

    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setattr(settings, "AI_API_KEY", "test-master-key-admin-accounts")
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(admin_module, "async_session_factory", db_factory)


@pytest.fixture
def auth_mock_response():
    return {
        "user_info": {
            "username": "alice",
            "status": "Active",
            "exp_date": "1893456000",
            "max_connections": "3",
            "allowed_output_formats": ["m3u8", "ts", "mp4"],
        },
        "server_info": {
            "url": "http://prov.example",
            "port": "8080",
            "https_port": "8443",
        },
    }


# ─── 1. Authentification Basic Auth (401) ────────────────────────────────────


class TestAdminAccountsAuthGuard:
    async def test_get_accounts_requires_auth(self, api_client):
        resp = await api_client.get("/admin/accounts")
        assert resp.status_code == 401

    async def test_get_accounts_table_requires_auth(self, api_client):
        resp = await api_client.get("/admin/accounts/table")
        assert resp.status_code == 401

    async def test_post_accounts_requires_auth(self, api_client):
        resp = await api_client.post("/admin/accounts", data={"label": "Test"})
        assert resp.status_code == 401

    async def test_post_test_connection_requires_auth(self, api_client):
        resp = await api_client.post("/admin/accounts/fake_id/test")
        assert resp.status_code == 401

    async def test_post_toggle_requires_auth(self, api_client):
        resp = await api_client.post("/admin/accounts/fake_id/toggle")
        assert resp.status_code == 401

    async def test_get_categories_requires_auth(self, api_client):
        resp = await api_client.get("/admin/accounts/fake_id/categories")
        assert resp.status_code == 401

    async def test_post_categories_mode_requires_auth(self, api_client):
        resp = await api_client.post("/admin/accounts/fake_id/categories/mode")
        assert resp.status_code == 401

    async def test_post_category_toggle_requires_auth(self, api_client):
        resp = await api_client.post("/admin/accounts/fake_id/categories/cat_1/toggle")
        assert resp.status_code == 401


# ─── 2. Protection CSRF Sec-Fetch-Site (403) ──────────────────────────────────


class TestAdminAccountsCsrfGuard:
    async def test_post_accounts_cross_site_rejected(self, api_client):
        resp = await api_client.post(
            "/admin/accounts",
            data={"label": "Bad"},
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cross-site request rejected"

    async def test_post_test_connection_cross_site_rejected(self, api_client):
        resp = await api_client.post(
            "/admin/accounts/acc1/test",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403

    async def test_post_toggle_cross_site_rejected(self, api_client):
        resp = await api_client.post(
            "/admin/accounts/acc1/toggle",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403

    async def test_post_categories_mode_cross_site_rejected(self, api_client):
        resp = await api_client.post(
            "/admin/accounts/acc1/categories/mode",
            data={"filter_mode": "whitelist"},
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403

    async def test_post_category_toggle_cross_site_rejected(self, api_client):
        resp = await api_client.post(
            "/admin/accounts/acc1/categories/cat1/toggle",
            data={"category_type": "vod"},
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403

    async def test_post_categories_refresh_cross_site_rejected(self, api_client):
        resp = await api_client.post(
            "/admin/accounts/acc1/categories/refresh",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403

    async def test_post_categories_block_adult_cross_site_rejected(self, api_client):
        resp = await api_client.post(
            "/admin/accounts/acc1/categories/block-adult",
            auth=ADMIN_AUTH,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403


# ─── 3. Sécurité : ZÉRO mot de passe dans le HTML ─────────────────────────────


class TestAdminAccountsNoPasswordLeaked:
    async def test_accounts_listing_never_contains_password(
        self, api_client, db_factory
    ):
        secret = "VERY_CONFIDENTIAL_PASS_XYZ987"
        account_id = "safe_acc_1"
        async with db_factory() as session:
            account = XtreamAccount(
                id=account_id,
                label="Fournisseur Privé",
                base_url="http://prov.example",
                port=8080,
                username="alice_user",
                password=secret,
                status="Active",
                is_active=True,
                created_at=1700000000000,
            )
            session.add(account)
            await session.commit()

        # 1. Page complète
        resp_page = await api_client.get("/admin/accounts", auth=ADMIN_AUTH)
        assert resp_page.status_code == 200
        assert secret not in resp_page.text
        assert "alice_user" in resp_page.text
        assert "Fournisseur Privé" in resp_page.text

        # 2. Fragment de tableau
        resp_table = await api_client.get("/admin/accounts/table", auth=ADMIN_AUTH)
        assert resp_table.status_code == 200
        assert secret not in resp_table.text

        # 3. Éditeur de catégories
        resp_cats = await api_client.get(
            f"/admin/accounts/{account_id}/categories", auth=ADMIN_AUTH
        )
        assert resp_cats.status_code == 200
        assert secret not in resp_cats.text


# ─── 4. Création de compte Xtream ─────────────────────────────────────────────


class TestAdminAccountsCreate:
    async def test_create_account_success(
        self, api_client, xtream_mock, auth_mock_response, db_factory
    ):
        xtream_mock.get("http://prov.example:8080/player_api.php").respond(
            200, json=auth_mock_response
        )

        resp = await api_client.post(
            "/admin/accounts",
            data={
                "label": "Fournisseur Premium",
                "base_url": "http://prov.example",
                "port": 8080,
                "username": "alice",
                "password": "secret_password",
            },
            auth=ADMIN_AUTH,
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "refresh-accounts"
        assert "Fournisseur Premium" in resp.text
        assert "secret_password" not in resp.text

        # Vérifier en DB
        async with db_factory() as session:
            result = await session.execute(
                select(XtreamAccount).where(XtreamAccount.label == "Fournisseur Premium")
            )
            acc = result.scalars().first()
            assert acc is not None
            assert acc.username == "alice"
            assert acc.status == "Active"

    async def test_create_account_duplicate_conflict(
        self, api_client, xtream_mock, auth_mock_response
    ):
        xtream_mock.get("http://prov.example:8080/player_api.php").respond(
            200, json=auth_mock_response
        )

        data = {
            "label": "Fournisseur Un",
            "base_url": "http://prov.example",
            "port": 8080,
            "username": "duplicate_user",
            "password": "pass",
        }
        resp1 = await api_client.post(
            "/admin/accounts", data=data, auth=ADMIN_AUTH, headers=CSRF_HEADERS
        )
        assert resp1.status_code == 200

        # Deuxième tentative -> 409
        resp2 = await api_client.post(
            "/admin/accounts", data=data, auth=ADMIN_AUTH, headers=CSRF_HEADERS
        )
        assert resp2.status_code == 409
        assert "existe déjà" in resp2.text

    async def test_create_account_auth_failure(self, api_client, xtream_mock):
        xtream_mock.get("http://prov.example:8080/player_api.php").respond(
            401, text="Unauthorized"
        )

        resp = await api_client.post(
            "/admin/accounts",
            data={
                "label": "Bad Credentials",
                "base_url": "http://prov.example",
                "port": 8080,
                "username": "bad_user",
                "password": "bad_password",
            },
            auth=ADMIN_AUTH,
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
        assert "authentification" in resp.text.lower()
        assert "bad_password" not in resp.text

    async def test_create_account_validation_error_returns_422(
        self, api_client, monkeypatch
    ):
        from pydantic import ValidationError
        from app.api import admin as admin_module

        def _raise_val(*args, **kwargs):
            raise ValidationError.from_exception_data("AccountCreate", [])

        monkeypatch.setattr(admin_module, "AccountCreate", _raise_val)

        resp = await api_client.post(
            "/admin/accounts",
            data={
                "label": "Bad Account",
                "base_url": "http://prov.example",
                "port": 8080,
                "username": "bad_user",
                "password": "secret_password",
            },
            auth=ADMIN_AUTH,
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 422
        assert "Paramètres invalides" in resp.text


# ─── 5. Test de connexion Xtream ──────────────────────────────────────────────


class TestAdminAccountsTestConnection:
    async def test_test_connection_success(
        self, api_client, xtream_mock, auth_mock_response, db_factory
    ):
        xtream_mock.get("http://prov.example:8080/player_api.php").respond(
            200, json=auth_mock_response
        )

        acc_id = "test_conn_acc"
        async with db_factory() as session:
            session.add(
                XtreamAccount(
                    id=acc_id,
                    label="Test Conn",
                    base_url="http://prov.example",
                    port=8080,
                    username="alice",
                    password="my_password",
                    status="Unknown",
                )
            )
            await session.commit()

        resp = await api_client.post(
            f"/admin/accounts/{acc_id}/test", auth=ADMIN_AUTH, headers=CSRF_HEADERS
        )
        assert resp.status_code == 200
        assert "Connexion établie avec succès" in resp.text
        assert "Active" in resp.text
        assert "Connexions max :" in resp.text
        assert ">3<" in resp.text
        assert "my_password" not in resp.text

    async def test_test_connection_failure(self, api_client, xtream_mock, db_factory):
        xtream_mock.get("http://prov.example:8080/player_api.php").respond(500)

        acc_id = "test_fail_acc"
        async with db_factory() as session:
            session.add(
                XtreamAccount(
                    id=acc_id,
                    label="Fail Conn",
                    base_url="http://prov.example",
                    port=8080,
                    username="bob",
                    password="bob_password",
                    status="Unknown",
                )
            )
            await session.commit()

        resp = await api_client.post(
            f"/admin/accounts/{acc_id}/test", auth=ADMIN_AUTH, headers=CSRF_HEADERS
        )
        assert resp.status_code == 200
        assert "Échec du test de connexion" in resp.text
        assert "bob_password" not in resp.text


# ─── 6. Toggle actif / inactif ────────────────────────────────────────────────


class TestAdminAccountsToggle:
    async def test_toggle_active_status(self, api_client, db_factory):
        acc_id = "toggle_acc"
        async with db_factory() as session:
            session.add(
                XtreamAccount(
                    id=acc_id,
                    label="Toggle Test",
                    base_url="http://prov.example",
                    port=8080,
                    username="user1",
                    password="pwd",
                    is_active=True,
                )
            )
            await session.commit()

        # Actif -> Inactif
        resp1 = await api_client.post(
            f"/admin/accounts/{acc_id}/toggle", auth=ADMIN_AUTH, headers=CSRF_HEADERS
        )
        assert resp1.status_code == 200
        assert "Inactif" in resp1.text

        async with db_factory() as session:
            acc = await session.get(XtreamAccount, acc_id)
            assert acc.is_active is False

        # Inactif -> Actif
        resp2 = await api_client.post(
            f"/admin/accounts/{acc_id}/toggle", auth=ADMIN_AUTH, headers=CSRF_HEADERS
        )
        assert resp2.status_code == 200
        assert "Actif" in resp2.text

        async with db_factory() as session:
            acc = await session.get(XtreamAccount, acc_id)
            assert acc.is_active is True


# ─── 7. Éditeur de catégories ─────────────────────────────────────────────────


class TestAdminCategoriesEditor:
    async def test_categories_listing_and_mode_change(self, api_client, db_factory):
        acc_id = "cat_test_acc"
        async with db_factory() as session:
            session.add(
                XtreamAccount(
                    id=acc_id,
                    label="Categories Account",
                    base_url="http://prov.example",
                    port=8080,
                    username="cat_user",
                    password="pwd",
                    category_filter_mode="all",
                )
            )
            session.add(
                XtreamCategory(
                    account_id=acc_id,
                    category_id="10",
                    category_type="vod",
                    category_name="Films Action",
                    is_allowed=True,
                    last_fetched_at=1700000000000,
                )
            )
            session.add(
                XtreamCategory(
                    account_id=acc_id,
                    category_id="20",
                    category_type="vod",
                    category_name="VOD ADULT +18",
                    is_allowed=True,
                    last_fetched_at=1700000000000,
                )
            )
            await session.commit()

        # 1. Listing initial
        resp = await api_client.get(
            f"/admin/accounts/{acc_id}/categories", auth=ADMIN_AUTH
        )
        assert resp.status_code == 200
        assert "Films Action" in resp.text
        assert "VOD ADULT +18" in resp.text
        assert "🔞 +18" in resp.text
        assert "Tout autoriser" in resp.text

        # 2. Changement de mode vers whitelist
        resp_mode = await api_client.post(
            f"/admin/accounts/{acc_id}/categories/mode",
            data={"filter_mode": "whitelist"},
            auth=ADMIN_AUTH,
            headers=CSRF_HEADERS,
        )
        assert resp_mode.status_code == 200
        assert "Mode de filtrage mis à jour : WHITELIST" in resp_mode.text

        async with db_factory() as session:
            acc = await session.get(XtreamAccount, acc_id)
            assert acc.category_filter_mode == "whitelist"

    async def test_category_toggle(self, api_client, db_factory):
        acc_id = "cat_toggle_acc"
        async with db_factory() as session:
            session.add(
                XtreamAccount(
                    id=acc_id,
                    label="Cat Toggle Account",
                    base_url="http://prov.example",
                    port=8080,
                    username="user_t",
                    password="pwd",
                    category_filter_mode="whitelist",
                )
            )
            session.add(
                XtreamCategory(
                    account_id=acc_id,
                    category_id="42",
                    category_type="vod",
                    category_name="Comédie",
                    is_allowed=True,
                    last_fetched_at=1700000000000,
                )
            )
            await session.commit()

        # Toggle : Autorisé -> Bloqué
        resp_toggle = await api_client.post(
            f"/admin/accounts/{acc_id}/categories/42/toggle",
            data={"category_type": "vod"},
            auth=ADMIN_AUTH,
            headers=CSRF_HEADERS,
        )
        assert resp_toggle.status_code == 200
        assert "Exclue" in resp_toggle.text
        assert "Autoriser" in resp_toggle.text

        async with db_factory() as session:
            stmt = select(XtreamCategory).where(
                XtreamCategory.account_id == acc_id,
                XtreamCategory.category_id == "42",
            )
            res = await session.execute(stmt)
            cat = res.scalar_one()
            assert cat.is_allowed is False

    async def test_block_adult_categories(self, api_client, db_factory):
        acc_id = "adult_acc"
        async with db_factory() as session:
            session.add(
                XtreamAccount(
                    id=acc_id,
                    label="Adult Account",
                    base_url="http://prov.example",
                    port=8080,
                    username="user_a",
                    password="pwd",
                    category_filter_mode="blacklist",
                )
            )
            session.add(
                XtreamCategory(
                    account_id=acc_id,
                    category_id="100",
                    category_type="vod",
                    category_name="VOD Normale",
                    is_allowed=True,
                    last_fetched_at=1700000000000,
                )
            )
            session.add(
                XtreamCategory(
                    account_id=acc_id,
                    category_id="101",
                    category_type="vod",
                    category_name="VOD ADULTE XXX",
                    is_allowed=True,
                    last_fetched_at=1700000000000,
                )
            )
            await session.commit()

        resp = await api_client.post(
            f"/admin/accounts/{acc_id}/categories/block-adult",
            auth=ADMIN_AUTH,
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        assert "1 catégorie(s) adulte(s) bloquée(s)" in resp.text

        async with db_factory() as session:
            stmt = select(XtreamCategory).where(XtreamCategory.account_id == acc_id)
            res = await session.execute(stmt)
            cats = {c.category_id: c.is_allowed for c in res.scalars().all()}
            assert cats["100"] is True  # Non adulte reste autorisé
            assert cats["101"] is False  # Adulte est bloqué

    async def test_categories_mode_unknown_account_404(self, api_client):
        resp = await api_client.post(
            "/admin/accounts/unknown_acc_id/categories/mode",
            data={"filter_mode": "whitelist"},
            auth=ADMIN_AUTH,
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 404
        assert "Compte introuvable" in resp.text

    async def test_refresh_categories_success(
        self, api_client, xtream_mock, db_factory
    ):
        acc_id = "refresh_ok_acc"
        async with db_factory() as session:
            session.add(
                XtreamAccount(
                    id=acc_id,
                    label="Refresh OK Test",
                    base_url="http://prov.example",
                    port=8080,
                    username="user_ref",
                    password="pwd",
                    category_filter_mode="all",
                )
            )
            await session.commit()

        xtream_mock.get(
            "http://prov.example:8080/player_api.php?username=user_ref&password=pwd&action=get_vod_categories"
        ).respond(200, json=[{"category_id": "1", "category_name": "Action"}])
        xtream_mock.get(
            "http://prov.example:8080/player_api.php?username=user_ref&password=pwd&action=get_series_categories"
        ).respond(200, json=[{"category_id": "2", "category_name": "Drama"}])

        resp = await api_client.post(
            f"/admin/accounts/{acc_id}/categories/refresh",
            auth=ADMIN_AUTH,
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        assert "Catégories synchronisées : 1 VOD, 1 séries." in resp.text

        async with db_factory() as session:
            res = await session.execute(
                select(XtreamCategory).where(XtreamCategory.account_id == acc_id)
            )
            cats = res.scalars().all()
            assert len(cats) == 2

    async def test_refresh_categories_httpx_error_never_leaks_credentials(
        self, api_client, xtream_mock, db_factory
    ):
        acc_id = "refresh_fail_acc"
        secret_pass = "CONFIDENTIAL_REFRESH_PASSWORD_999"
        secret_user = "secret_xtream_user"
        async with db_factory() as session:
            session.add(
                XtreamAccount(
                    id=acc_id,
                    label="Refresh Leak Test",
                    base_url="http://prov.example",
                    port=8080,
                    username=secret_user,
                    password=secret_pass,
                    category_filter_mode="all",
                )
            )
            await session.commit()

        # Provider returns 500 (HTTPStatusError embeds URL with query params)
        xtream_mock.get("http://prov.example:8080/player_api.php").respond(500)

        resp = await api_client.post(
            f"/admin/accounts/{acc_id}/categories/refresh",
            auth=ADMIN_AUTH,
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200
        assert "Erreur lors du rafraîchissement" in resp.text
        assert secret_pass not in resp.text
        assert secret_user not in resp.text
