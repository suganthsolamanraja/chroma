from hypothesis import given, Phase, settings
import hypothesis.strategies as st
import pytest
from typing import Dict, Any, Tuple

from chromadb import AdminClient
from chromadb.api import AdminAPI, ServerAPI
from chromadb.config import Settings, System
from chromadb.test.auth.rbac_test_executors import api_executors
from chromadb.test.auth.strategies import (
    random_token_transport_header,
    rbac_test_conf,
    unauthorized_actions
)
from chromadb.test.conftest import _fastapi_fixture


def test_basic_authn_rbac_authz_unit_test(
        api_with_authn_rbac_authz: ServerAPI) -> None:
    api_with_authn_rbac_authz.create_collection('test')


def client_and_admin_client(
    settings: Settings
) -> Tuple[ServerAPI, AdminAPI]:
    system = System(settings)
    api = system.instance(ServerAPI)
    admin_api = AdminClient(api.get_settings())
    system.start()
    return api, admin_api


@settings(max_examples=10, phases=[Phase.generate, Phase.target])
@given(
    rbac_test_conf(),
    st.booleans(),
    random_token_transport_header(),
    st.data()
)
def test_token_authn_rbac_authz(
    rbac_conf: Dict[str, Any],
    persistence: bool,
    header: str | None,
    data: Any
) -> None:
    for user in rbac_conf["users"]:
        if user["id"] == "__root__":
            break

        token_index = data.draw(
            st.integers(
                min_value=0,
                max_value=len(user["tokens"]) - 1
            )
        )
        token = user["tokens"][token_index]

        api_fixture = _fastapi_fixture(
            is_persistent=persistence,
            chroma_auth_token_transport_header=header,
            chroma_client_auth_credentials=token,
            chroma_client_auth_provider="chromadb.auth."
            "token_authn.TokenAuthClientProvider",

            chroma_server_authn_provider="chromadb.auth.token_authn."
            "TokenAuthenticationServerProvider",
            chroma_server_authn_credentials_file=rbac_conf["filename"],
            chroma_server_authz_provider="chromadb.auth.simple_rbac_authz."
            "SimpleRBACAuthorizationProvider",
            chroma_server_authz_config_file=rbac_conf["filename"],
        )
        sys: System = next(api_fixture)
        sys.reset_state()
        api = sys.instance(ServerAPI)

        root_settings = Settings(**dict(sys.settings))
        root_users = [
            user for user in rbac_conf["users"] if user["id"] == "__root__"
        ]
        assert len(root_users) == 1
        root_user = root_users[0]
        root_settings.chroma_client_auth_credentials = root_user[
            "tokens"
        ][0]
        root_api, admin_api = client_and_admin_client(root_settings)

        role_matches = [
            r for r in rbac_conf["roles"] if r["id"] == user["role"]]
        assert len(role_matches) == 1
        role = role_matches[0]

        for action in role["actions"]:
            api_executors[action](
                api,
                admin_api,
                root_api,
            )

        for unauthorized_action in unauthorized_actions(role["actions"]):
            with pytest.raises(Exception) as ex:
                api_executors[unauthorized_action](
                    api,
                    admin_api,
                    root_api,
                )
                assert "Unauthorized" in str(ex) or "Forbidden" in str(ex)
