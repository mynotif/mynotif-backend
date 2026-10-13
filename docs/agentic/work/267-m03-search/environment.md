# Environnement — #267

Branche feat/267-m03-search ; worktree isolé 267-m03-search à base 987eab9.
`make virtualenv/test` avec uv 0.11.12 / Python 3.13; aucune copie de .env.
`scripts/m03-synthetic.sh` lance les commandes avec env -i : aucun secret hérité,
SQLite en mémoire, réseau AWS désactivé par défaut, clé de
signature publique synthétique, sans DSN Sentry ni intégration réelle.
