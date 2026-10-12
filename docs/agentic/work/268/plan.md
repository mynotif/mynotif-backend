# Plan — #268

PLAN: PASS. Complexité élevée, LARGE ; modèle courant disponible hérité, revue indépendante High recommandée. Budget non observable. Critère arrêt : PR prête pour revue, CI et Lead Tech final sur SHA exact, aucun merge.

1. Créer nurse/read_serializers.py (schémas fermés, validation query), read_api.py (base API privée, erreurs, pagination SQL), read_views.py et read_urls.py ; enregistrer sous v2 main.urls. Dépendance #267 search_normalized ; aucune modification v1/M02.
2. Ajouter contexte/redaction de logs scoped aux lectures, callbacks Sentry sans PII, configuration Gunicorn sûre ; tests marqueur synthétique et DEBUG SQL. Ne déclarer aucune conformité d'infrastructure réelle.
3. pytest synthétiques ≥21 patients deux comptes, auth absent/invalide/expiré/révoqué/Token, droits staff/absence profil, schema, global search Unicode/littéraux et query counts/SQL LIMIT. Page 100/101/0/invalide/doublon/inconnue, HEAD/OPTIONS/cache, v1/M02 régressions et OpenAPI.
4. make unittest, make lint, make run/migrations/check via environnement jetable ; HTTP loopback synthétique pour preuve routes. Documenter commandes/résultats/SHA/limites dans Verify.
5. Reviewer indépendant lecture seule puis un commit Refs #268 ; PR empilée base feat/267-m03-search ; CI finale puis Lead Tech distinct. Rapports historiques committés seulement avant SHA final, ultime attestation hors commit.

Isolation : worktrees/268-m03-patients, branche feat/268-m03-patients, root seul écrivain ; agent recherche/implémentation #267 dans son propre worktree, contrat champs stabilisé. Aucun test stockage réel. Aucun changement schema en #268. Risques dénormalisation traités #267 ; arrêt activation réelle tant qu'anciens écrivains/backfill non vérifiés. Rollback routes avant colonnes, garder v1.

Gate PASS : fichiers/steps/tests/sécurité/dépendances définis. Fondation et autorisation migration ne sont pas présumées livrées ; aucune publication avant leurs preuves.
