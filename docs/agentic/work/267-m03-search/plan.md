# Plan — backend #267

PASS. Confirmation explicite lock_timeout PostgreSQL 2 s acquise.

1. Isoler branche feat/267-m03-search, worktree dédié, Python 3.13/uv 0.11.12.
2. Écrire normalisation, queryset de recherche, maintenance save/update/bulk;
   figer schéma serializer v1 et ajouter tests Unicode/écritures/concurrence.
3. Après confirmation 2s seulement, ajouter modèle et migrations 0014/0015;
   commande réparation keyset bornée et tests migration ancienne→nouvelle.
4. Exécuter pytest ciblé puis make unittest/lint/migrations/check, sqlmigrate;
   documenter commandes et résultats réellement observés.
5. Revue indépendante avant commit, puis un commit Refs: #267 et une PR;
   Lead Tech sur tête finale avec CI verte. Pas de merge ni déploiement.

Seul écrivain : agent normalization_research. Reviewer indépendant orchestré
par root. Aucune donnée réelle, migration de production, dépendance ou refactor.
