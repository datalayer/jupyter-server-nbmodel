# Copyright (c) 2023-2024 Datalayer, Inc.
#
# BSD 3-Clause License

SHELL=/bin/bash

.DEFAULT_GOAL := default

CONDA_ACTIVATE=source $$(conda info --base)/etc/profile.d/conda.sh ; conda activate
CONDA_DEACTIVATE=source $$(conda info --base)/etc/profile.d/conda.sh ; conda deactivate
CONDA_REMOVE=source $$(conda info --base)/etc/profile.d/conda.sh ; conda remove -y --all -n

ENV_NAME=datalayer

.SILENT: init install

.PHONY: build publish

help: ## display this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

default: help ## default target is help

install: ## install
	npm install

start: ## start
	npm start

build: ## build
	npm run build

deploy: build ## deploy
	aws s3 rm \
		s3://datalayer-jupyter-server-nbmodel/ \
		--recursive && \
		aws s3 cp \
		./build \
		s3://datalayer-jupyter-server-nbmodel/ \
		--recursive && \
		aws cloudfront create-invalidation \
		--distribution-id E1YHLNGDXV5LKB \
		--paths "/*"
echo open ✨  https://jupyter-server-nbmodel.datalayer.tech
