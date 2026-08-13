<!--
  ~ Copyright (c) 2023-2024 Datalayer, Inc.
  ~
  ~ BSD 3-Clause License
-->

[![Datalayer](https://assets.datalayer.tech/datalayer-25.svg)](https://datalayer.io)

[![Become a Sponsor](https://img.shields.io/static/v1?label=Become%20a%20Sponsor&message=%E2%9D%A4&logo=GitHub&style=flat&color=1ABC9C)](https://github.com/sponsors/datalayer)

# Jupyter Server Nbmodel Docs

> Source code for the [Jupyter Server Nbmodel Documentation](https://jupyter-server-nbmodel.datalayer.tech), built with [Docusaurus](https://docusaurus.io).

```bash
# Install the dependencies.
make install
```

Run the python and typescript API docs by running in the reposiroty root.

```bash
make pydoc
make typedoc
```


```bash
# Local Development: This command starts a local development server and opens up a browser window.
# Most changes are reflected live without having to restart the server.
echo open http://localhost:3000
make start
```

```bash
# Build: This command generates static content into the `build` directory 
# and can be served using any static contents hosting service.
make build
```

```bash
# Publish if you have karma for.
make publish
```
