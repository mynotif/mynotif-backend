# MyNotif with Django

[![Tests](https://github.com/issa-diallo/Mynotif_backend/actions/workflows/tests.yml/badge.svg)](https://github.com/issa-diallo/Mynotif_backend/actions/workflows/tests.yml)

## URLs

- :tada: https://mynotif-api.herokuapp.com/
- :memo: https://mynotif-api.herokuapp.com/swagger/
- :memo: https://mynotif-api.herokuapp.com/redoc/
- :goal_net: https://sentry.io/organizations/andre-5t/issues/3096848907/?project=6257099

## Install
```sh
make virtualenv/test
cp .env.example .env
```

## :tada: run
```sh
make run/dev
```

## :test_tube: test
```sh
make unittest
```

## :rotating_light: linting
```sh
make lint
make format
```

## Requirements bump
```sh
make -B requirements.txt
```
The project has automated handling of production requirements, the idea behind it is that
you should always use the latest versions of every requirement.
`pip-compile` is used to handle it.

## Docker
```sh
make docker/build
make docker/run
```

## Learn More

You can learn more in the api rest framework(https://www.django-rest-framework.org/) 

You can learn more build API(https://insomnia.rest/)

You can learn more in django(https://www.djangoproject.com/)
