# Verify — backend #267

Implémentation et migrations autorisées : utilisateur a confirmé lock_timeout
PostgreSQL 2 s. Aucune migration production exécutée. Base
`987eab9753b562be9eedd3f8952eb3d14a9fb81a` + diff non commité; revue indépendante,
commit unique, CI et Lead Tech sur tête finale restent à effectuer par root.

## Environnement et commandes réellement exécutées

Python 3.13.1 / uv 0.11.12, worktree 267-m03-search, aucun .env ni secret hérité.
Le runner env -i refuse les dotenv des ancêtres; DB synthétiques exclusivement.

```sh
make virtualenv/test
scripts/m03-synthetic.sh venv/bin/pytest src/tests/nurse/test_patient_search.py src/tests/nurse/test_patient_search_migrations.py -q
scripts/m03-synthetic.sh make unittest
scripts/m03-synthetic.sh make lint
scripts/m03-synthetic.sh make run/migrations/check
docker pull postgres:16
docker run --rm --detach --name ordopro-m03-search-267 --publish 127.0.0.1::5432 --tmpfs /var/lib/postgresql/data:rw --env POSTGRES_DB=m03_synthetic --env POSTGRES_USER=m03_synthetic --env POSTGRES_PASSWORD=m03_synthetic postgres:16
docker port ordopro-m03-search-267 5432
docker exec ordopro-m03-search-267 pg_isready -U m03_synthetic -d m03_synthetic
```

Le port dynamique lors de cette exécution finale : 127.0.0.1:32769. Les commandes
suivantes emploient seulement des credentials publics synthétiques :

```sh
scripts/m03-synthetic.sh env DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_synthetic DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/pytest src/tests/nurse/test_patient_search.py src/tests/nurse/test_patient_search_migrations.py src/tests/nurse/test_nurse_access.py src/tests/main/test_session_api.py -q
scripts/m03-synthetic.sh env DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_synthetic DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/pytest src/tests/nurse/test_patient_search_postgres.py -q
scripts/m03-synthetic.sh env DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_synthetic DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/pytest src/tests/main src/tests/nurse -q
scripts/m03-synthetic.sh env DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_synthetic DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/python src/manage.py sqlmigrate nurse 0014_patient_search_columns
scripts/m03-synthetic.sh env DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_synthetic DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/python src/manage.py sqlmigrate nurse 0014_patient_search_columns --backwards
scripts/m03-synthetic.sh venv/bin/python src/manage.py sqlmigrate nurse 0014_patient_search_columns
```

## Résultats observés

- SQLite ciblé, sans plugin temporaire : 31 passed en 1.72s; les quatre tests
  migrations ancienne→nouvelle, source CAS, lots keyset500 et timeout passent.
- Suite normale make unittest finale sans plugin : **285 passed, 6 skipped,
  11 warnings en 128.58s**, coverage94%. Les skips sont cinq tests de concurrence
  réservés PostgreSQL et la qualification RLS PostgreSQL.
- PostgreSQL ciblé : 60 passed, 1 skipped en 10.50s. Le skip concerne le test
  source NUL SQLite, car PostgreSQL text interdit ce caractère.
- Qualification RLS PostgreSQL : 1 passed en 1.33s. Rôle synthétique
  NOSUPERUSER/NOBYPASSRLS voit zéro ligne sous FORCE RLS sans policy. Le superuser
  synthétique voit A+B sans scope, puis uniquement A avec filtrage ORM Nurse.
  Aucun changement de policy; flags originaux restaurés et rôle test supprimé.
- Lint PASS (isort/flake8/ruff/terraform/prettier).
- Contrôle migrations PASS, No changes detected.
- SQL PostgreSQL réel forward/backward dans sql/ : transaction BEGIN/COMMIT,
  SET LOCAL lock_timeout2s avant chaque ADD/DROP. Colonnes text NULL sans défaut,
  index ou contrainte supplémentaire. SQL SQLite également collecté.

StrIndex (INSTR SQLite/STRPOS PostgreSQL) garantit les caractères %, _, *,
backslash et NUL SQLite littéraux. LIKE SQLite tronquait les motifs au premier
NUL; ce cas a motivé le choix SQL natif et est couvert. PostgreSQL text n'admettant
pas NUL, un terme NUL retourne none sans paramètre lié à la DB.

## Limites et reprise

Ce ticket prépare le schéma/recherche, sans enregistrer les routes v2 à lui seul.
Aucun SLA, volume production, rôle DB production, journal proxy/hébergement ou
navigation Android après deuxième401 n'est validé ici. Les tests superuser
synthétique ne qualifient pas le rôle opérationnel : le RLS historique peut
refuser les lectures sans policy à un rôle ordinaire. Aucun élargissement RLS
n'est inclus pour masquer cette limite.

Retirer les anciens écrivains puis repair_patient_search + --check avant toute
activation v2; backfill initial seul insuffisant pendant rolling deploy.
Rollback : retirer routes d'abord, conserver colonnes. Aucun merge/déploiement.

## Élargissement PostgreSQL et défauts de tests préexistants

La commande complète main+nurse PostgreSQL est réellement exécutée : **236 passed,
23 failed, 1 skipped en 117.38s**. Aucun de ces échecs n'est caché : 22 assertions
anciennes de test_views supposent des ids égaux à1 (les séquences PostgreSQL ne
reviennent pas en arrière après rollback). La concurrence registration retourne
429/429 après les appels registration précédents conservés dans le cache throttle.
Aucune correction annexe de tests/quotas/séquences n'est incluse dans #267.

Reproduction sur la base intacte 987eab9 exportée dans /tmp, mêmes DB/credentials
synthétiques : test_models puis test_views::TestPatient::test_patient_detail
produisent **13 passed, 1 failed** (404 pour chemin pk1), et TestUserCreate +
TestUserCreateV2 puis concurrence registration produisent **7 passed, 2 failed**,
dont le même 429/429. La concurrence M02 isolée sur la version modifiée passe
**5 tests en 9.55s**. Ces résultats distinguent la fragilité ancienne des tests
groupés et la preuve ciblée des contrats préparés.

```sh
mkdir -p /tmp/m03-267-baseline-987eab9
git archive 987eab9753b562be9eedd3f8952eb3d14a9fb81a | tar -x -C /tmp/m03-267-baseline-987eab9
scripts/m03-synthetic.sh env PYTHONPATH=/tmp/m03-267-baseline-987eab9/src DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_synthetic DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/pytest /tmp/m03-267-baseline-987eab9/src/tests/nurse/test_models.py /tmp/m03-267-baseline-987eab9/src/tests/nurse/test_views.py::TestPatient::test_patient_detail -q
scripts/m03-synthetic.sh env PYTHONPATH=/tmp/m03-267-baseline-987eab9/src DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_synthetic DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/pytest /tmp/m03-267-baseline-987eab9/src/tests/nurse/test_views.py::TestUserCreate /tmp/m03-267-baseline-987eab9/src/tests/nurse/test_views.py::TestUserCreateV2 /tmp/m03-267-baseline-987eab9/src/tests/main/test_session_concurrency.py::test_concurrent_registration_enforces_unique_login -q
scripts/m03-synthetic.sh env DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_synthetic DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/pytest src/tests/main/test_session_concurrency.py -q
```

## Batterie PostgreSQL finale ciblée

Sur le code final préparé, **72 passed, 1 skipped, 2 warnings en 25.70s** :
normalisation/recherche/save/bulk/réparation, migrations ancienne→nouvelle/CAS/lots,
qualification RLS avec deux comptes et Nurses A/B, accès nurse v1 et sessions M02
API/services/concurrence. Le skip est uniquement le caractère source NUL SQLite.
Le test RLS final montre le rôle ordinaire refusé sous FORCE/no-policy, puis les
scopes ORM A→A et B→B sous superuser synthétique. Aucun policy élargi.

```sh
scripts/m03-synthetic.sh env DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_synthetic DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/pytest src/tests/nurse/test_patient_search.py src/tests/nurse/test_patient_search_migrations.py src/tests/nurse/test_patient_search_postgres.py src/tests/nurse/test_nurse_access.py src/tests/main/test_session_api.py src/tests/main/test_session_services.py src/tests/main/test_session_concurrency.py -q
scripts/m03-synthetic.sh make lint
scripts/m03-synthetic.sh make run/migrations/check
git diff --check
```

Les trois contrôles finaux passent; makemigrations affiche No changes detected.
Le conteneur synthétique reste disponible pour root/reviewer, sans données réelles
ni volume durable; sa suppression relève seulement du cleanup local autorisé.
Revue indépendante et CI sur commit final ne sont pas encore revendiquées.
