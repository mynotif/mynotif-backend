VIRTUAL_ENV?=venv
VERSION=$(shell git describe --tags --always)
PIP=$(VIRTUAL_ENV)/bin/pip
PIP_COMPILE=$(VIRTUAL_ENV)/bin/pip-compile
GUNICORN=$(VIRTUAL_ENV)/bin/gunicorn
PYTHON_MAJOR_VERSION=3
PYTHON_MINOR_VERSION=13
PYTHON_VERSION=$(PYTHON_MAJOR_VERSION).$(PYTHON_MINOR_VERSION)
PYTHON_WITH_VERSION=python$(PYTHON_VERSION)
PYTHON=$(VIRTUAL_ENV)/bin/python
ISORT=$(VIRTUAL_ENV)/bin/isort
FLAKE8=$(VIRTUAL_ENV)/bin/flake8
RUFF=$(VIRTUAL_ENV)/bin/ruff
PYTEST=$(VIRTUAL_ENV)/bin/pytest
NODE_PRETTIER=npx prettier
PORT?=8000
IMAGE_TAG=latest
AWS_ACCOUNT_ID=332944743618
DEPLOYMENT_AUTOMATION_ROLE=arn:aws:iam::$(AWS_ACCOUNT_ID):role/mynotif-deployment-automation-role
SESSION_NAME=deployment-session
AWS_PROFILE?=default
REGION=eu-west-3
REGISTRY=$(AWS_ACCOUNT_ID).dkr.ecr.$(REGION).amazonaws.com
APP_NAME=mynotif-backend
IMAGE_NAME=$(APP_NAME)-production
DOCKER_IMAGE=$(REGISTRY)/$(IMAGE_NAME)
SOURCES=src/
APPRUNNER_ARN=arn:aws:apprunner:$(REGION):$(AWS_ACCOUNT_ID):service/$(APP_NAME)-runner-production/18bebc83a64f4823a5dfbcbb8c98bfbe


all: virtualenv

$(VIRTUAL_ENV):
	$(PYTHON_WITH_VERSION) -m venv $(VIRTUAL_ENV)
	$(PYTHON) -m pip install --upgrade pip setuptools

virtualenv: $(VIRTUAL_ENV)
	$(PIP) install --requirement requirements.txt

virtualenv/test: virtualenv
	$(PIP) install -e .[dev]

requirements.txt: | $(VIRTUAL_ENV)
	$(PYTHON) -m pip install --upgrade pip-tools
	$(PIP_COMPILE) --upgrade --no-strip-extras --output-file $@

src/lambdas/requirements.txt: | $(VIRTUAL_ENV)
	$(PYTHON) -m pip install --upgrade pip-tools
	$(PIP_COMPILE) src/lambdas/requirements.in --upgrade --no-strip-extras --output-file $@

clean:
	rm -rf venv/ .pytest_cache/

unittest:
	$(PYTEST) --doctest-modules --cov src/ --cov-report term --cov-report html --cov-report xml src/

lint/isort:
	$(ISORT) --check-only --diff $(SOURCES)

lint/flake8:
	$(FLAKE8) $(SOURCES)

lint/ruff:
	$(RUFF) format --check $(SOURCES)

lint/terraform:
	terraform -chdir=terraform fmt -recursive -diff -check

lint/nodeprettier:
	$(NODE_PRETTIER) --check *.md *.yml .github/ src/

lint: lint/isort lint/flake8 lint/ruff lint/terraform lint/nodeprettier

format/isort:
	$(ISORT) $(SOURCES)

format/ruff:
	$(RUFF) format $(SOURCES)

format/terraform:
	terraform -chdir=terraform fmt -recursive -diff

format/nodeprettier:
	$(NODE_PRETTIER) --write *.md *.yml .github/ src/

format: format/isort format/ruff format/terraform format/nodeprettier

test: unittest lint

run/collectstatic: virtualenv
	$(PYTHON) src/manage.py collectstatic --noinput

run/migrations/create: virtualenv
	$(PYTHON) src/manage.py makemigrations

run/migrations/check: virtualenv
	$(PYTHON) src/manage.py makemigrations --check

run/migrations/apply: virtualenv
	$(PYTHON) src/manage.py migrate --noinput

run/dev: virtualenv
	$(PYTHON) src/manage.py runserver

run/prod:
	$(GUNICORN) --chdir src --bind 0.0.0.0:$(PORT) main.wsgi

docker/build:
	docker build --build-arg PORT=$(PORT) --build-arg VERSION=$(VERSION) --tag=$(DOCKER_IMAGE):$(IMAGE_TAG) .

docker/run:
	docker run -it --env-file .env --publish $(PORT):$(PORT) --rm $(DOCKER_IMAGE):$(IMAGE_TAG)

docker/run/sh:
	docker run -it --env-file .env --rm $(DOCKER_IMAGE):$(IMAGE_TAG) sh

docker/login: devops/aws/assume-role
	aws ecr get-login-password --region $(REGION) | docker login --username AWS --password-stdin $(AWS_ACCOUNT_ID).dkr.ecr.$(REGION).amazonaws.com

docker/push:
	docker push $(DOCKER_IMAGE):$(IMAGE_TAG)

devops/aws/assume-role:
	$(eval CREDENTIALS=$(shell aws sts assume-role \
    --role-arn $(DEPLOYMENT_AUTOMATION_ROLE) \
    --role-session-name deployment-session \
    --query "Credentials.[AccessKeyId,SecretAccessKey,SessionToken]" \
    --output text))
	$(eval export AWS_ACCESS_KEY_ID=$(word 1, $(CREDENTIALS)))
	$(eval export AWS_SECRET_ACCESS_KEY=$(word 2, $(CREDENTIALS)))
	$(eval export AWS_SESSION_TOKEN=$(word 3, $(CREDENTIALS)))

devops/terraform/init:
	terraform -chdir=terraform init

devops/terraform/plan:
	terraform -chdir=terraform plan

devops/terraform/apply:
	terraform -chdir=terraform apply -auto-approve

devops/terraform/destroy:
	terraform -chdir=terraform destroy -auto-approve

devops/aws/redeploy/apprunner:
	aws apprunner start-deployment --service-arn $(APPRUNNER_ARN) --region $(REGION)
