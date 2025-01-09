import pytest

pytest_plugins = ("pytest_jupyter.jupyter_server","jupyter_server_ydoc.pytest_plugin")


@pytest.fixture
def jp_server_config(jp_server_config):
    return {
        "ServerApp": {
            "jpserver_extensions": {
                "jupyter_server_ydoc": True,
                "jupyter_server_nbmodel": True,
                "jupyter_server_fileid": True, 
            },
            'IdentityProvider': {'token': ''},
            "disable_check_xsrf": True,
        },
    }
