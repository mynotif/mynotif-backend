# Handoff — backend #267

Code et migrations prêts à revue indépendante; aucun commit ni PR créé par cet
agent. Lock_timeout2s confirmé explicitement par l'utilisateur; migrations préparées et éprouvées sur SQLite/PostgreSQL synthétiques. Les validations finales sont documentées dans verify.md.

Implémenté : quatre colonnes privées/manager/mixin, save deferred/update_fields,
gardes update/bulk, normalisation NFD/retrait M/casefold, recherche SQL littérale
INSTR/STRPOS, serializer v1 figé, réparation/check CAS keyset borné, runner propre.
0014 nullable sans défaut/index pose timeout local avant chaque ADD/DROP PG;
0015 historique figé non atomique, lots500/sourceCAS/reverse noop.

Tests : SQLite normal sans plugin, migrations anciennes→nouvelles/CAS/bornes,
PostgreSQL natif recherche et migrations, v1/M02, rôle RLS synthétique sans bypass
refusé sous FORCE/no-policy et isolation ORM A/B sous bypass synthétique.
SQL réel forward/reverse collecté dans sql/. Aucun changement de policy RLS.

Root : revue indépendante avant commit unique Refs:#267; tester SHA final, CI,
Lead Tech, publier PR sans merge/deploiement. Conteneur PG synthétique disponible
sur loopback32769 (commande reproductible verify.md; tmpfs, pas de volume durable).

Risques : anciens écrivains/SQL brut ne maintiennent pas dérivés; retirer anciens
écrivains puis réparation/check avant activation. Le rôle réel et les journaux
DB/proxy/hébergement restent à qualifier. RLS historique FORCE sans policy peut
refuser lectures à rôle ordinaire; tests superuser ne prouvent pas production.
Rollback : retirer routes v2 d'abord et conserver colonnes. Tickets mobiles
#70/#71 et parent#10 restent ouverts; navigation mobile deuxième401 hors périmètre.

Suite large PostgreSQL main+nurse :23 échecs de tests préexistants (ids1 figés,
cachethrottle avant concurrence), reproduits sur base987eab9; voir verify.md.
La suite SQLite normale est verte; le périmètre migrations/recherche/nurse accès
et sessions M02 dispose aussi d'une batterie PostgreSQL native ciblée verte.
Ne pas déclarer toute la suite PostgreSQL verte ni masquer ces limites.
