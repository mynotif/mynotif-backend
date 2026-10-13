# ADR — normalisation exacte et colonnes dérivées (#267)

Statut : décision retenue; lock_timeout PostgreSQL 2 s confirmé explicitement.
Contrat : [lectures v2](https://github.com/mynotif/ordopro_mobile/blob/main/docs/agentic/work/OP-M03/contracts-v2.md).

Contexte : le contrat prescrit NFD, retrait catégories Unicode M puis casefold;
la recherche substring porte sur quatre champs distincts, AND entre termes,
OR entre champs. Django 5.1.4 dessert PostgreSQL et SQLite synthétique.

Décision : quatre TextField nullable privés, maintenus à l'écriture par modèle
et queryset; requêtes substring SQL paramétrées (INSTR/STRPOS), aucun scan Python lors des lectures.
Pas d'index initial supplémentaire : un B-tree ne traite pas le motif `%terme%`.

Alternatives rejetées : lower/unaccent (normalisation différente des ligatures,
ß et des catégories Unicode), fonction native SQLite (pas de normalisation
Unicode complète), PL/Python/extension DB (dépendance infrastructure évitable),
concaténation de champs (matches qui traversent les frontières des champs),
filtrage de patients en Python (interdit par contrat, mémoire non bornée).

Conséquences : colonnes et migration supplémentaires, maintenance sur toutes les
écritures ORM et rejet des expressions non normalisables. Les SQL bruts et
anciennes instances ne maintiennent pas l'invariant. Activation v2 seulement
après retrait des anciens écrivains, réparation bornée puis contrôle réussi.
Rollback des routes avant tout changement du schéma; conserver les colonnes.
Tests PostgreSQL et SQLite synthétiques nécessaires; aucune preuve de volume
production ni SLA n'est revendiquée.
