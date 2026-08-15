# Copyright (c) 2024-2026 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

"""The output-recovery switch: read where JupyterLab reads, write what it writes.

The endpoint is a bridge to the user-settings file of the
``notebook-cell-executor`` plugin, so the contract worth pinning is the file:
the schema default when it is absent, the stored value when present, comments
tolerated on read, and a flip persisted where the lab will find it.
"""

import json

import pytest

from jupyter_server_nbmodel import handlers as h


@pytest.fixture()
def settings_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("JUPYTERLAB_SETTINGS_DIR", str(tmp_path))
    return tmp_path


def _settings_file(settings_dir):
    return (
        settings_dir
        / h.SETTINGS_PLUGIN_PACKAGE
        / f"{h.SETTINGS_PLUGIN_SCHEMA}.jupyterlab-settings"
    )


async def test_get_answers_the_schema_default_when_unset(settings_dir, jp_fetch):
    response = await jp_fetch("api", "nbmodel", "settings", "output-recovery")
    assert json.loads(response.body) == {"outputRecovery": False}


async def test_get_reads_the_lab_settings_file(settings_dir, jp_fetch):
    path = _settings_file(settings_dir)
    path.parent.mkdir(parents=True)
    path.write_text(
        "{\n  // switched on by hand in the lab editor\n  \"outputRecovery\": true\n}"
    )
    response = await jp_fetch("api", "nbmodel", "settings", "output-recovery")
    assert json.loads(response.body) == {"outputRecovery": True}


async def test_a_non_boolean_in_the_file_answers_the_default(settings_dir, jp_fetch):
    # "false" is a truthy string: anything but a boolean must read as the
    # schema default, never as enabled.
    path = _settings_file(settings_dir)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"outputRecovery": "false"}))
    response = await jp_fetch("api", "nbmodel", "settings", "output-recovery")
    assert json.loads(response.body) == {"outputRecovery": False}


async def test_a_non_object_settings_file_answers_the_default(settings_dir, jp_fetch):
    path = _settings_file(settings_dir)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(["not", "an", "object"]))
    response = await jp_fetch("api", "nbmodel", "settings", "output-recovery")
    assert json.loads(response.body) == {"outputRecovery": False}


async def test_put_persists_where_the_lab_reads(settings_dir, jp_fetch):
    response = await jp_fetch(
        "api",
        "nbmodel",
        "settings",
        "output-recovery",
        method="PUT",
        body=json.dumps({"outputRecovery": True}),
    )
    assert json.loads(response.body) == {"outputRecovery": True}
    stored = json.loads(_settings_file(settings_dir).read_text())
    assert stored["outputRecovery"] is True

    response = await jp_fetch("api", "nbmodel", "settings", "output-recovery")
    assert json.loads(response.body) == {"outputRecovery": True}


async def test_put_keeps_the_other_settings(settings_dir, jp_fetch):
    path = _settings_file(settings_dir)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"somethingElse": 3}))
    await jp_fetch(
        "api",
        "nbmodel",
        "settings",
        "output-recovery",
        method="PUT",
        body=json.dumps({"outputRecovery": True}),
    )
    assert json.loads(path.read_text()) == {"somethingElse": 3, "outputRecovery": True}


async def test_put_refuses_a_non_boolean(settings_dir, jp_fetch):
    from tornado.httpclient import HTTPClientError

    with pytest.raises(HTTPClientError) as error:
        await jp_fetch(
            "api",
            "nbmodel",
            "settings",
            "output-recovery",
            method="PUT",
            body=json.dumps({"outputRecovery": "yes"}),
        )
    assert error.value.code == 400
