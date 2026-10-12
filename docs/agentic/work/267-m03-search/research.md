# Research — backend #267

PASS (existing project, STANDARD). Base `987eab9`; ticket backend #267,
mobile #71/#70, contrat normatif mobile `OP-M03/contracts-v2.md` lu intégralement.
Architecture observée : Django 5.1.4, SQLite synthétique/PostgreSQL, Patient et
Nurse M2M, v1 PatientSerializer dynamique `__all__`; sessions M02 existantes.
Les routes v2 sont hors de ce ticket et restent non disponibles ici.

NFD + retrait de toutes catégories Unicode M + casefold doit être identique
sur requête et quatre champs. lower/unaccent n'est pas équivalent (ß, ligatures,
marques Unicode). Quatre colonnes dérivées TextField privées sont nécessaires;
pas de nouvel index B-tree pour des recherches contains `%terme%`.
Risques : fuite des champs via __all__, écritures bulk/update contournant save,
anciens écrivains en rolling deploy, concurrence du backfill, logs SQL DEBUG.
Contraintes : schéma v1 figé, maintenance des dérivés sur écritures ORM,
backfill borné et conditionnel, aucune activation v2 avant réparation validée.
Données et services exclusivement synthétiques, aucune migration production.
