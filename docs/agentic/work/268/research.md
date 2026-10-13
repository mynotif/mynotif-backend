# Research — #268 patients v2

RESEARCH: PASS. Entrée EXISTING, mode LARGE : isolation de données sensibles/auth ; contrat déjà conçu par mobile #72/PR #73. Phases produit hors périmètre. Base observée 987eab9753b562be9eedd3f8952eb3d14a9fb81a.

Contrat complet lu via gh api, blob 821310cbe0da90aa58a181a6c98cf2458ba13e3f : https://github.com/mynotif/ordopro_mobile/blob/main/docs/agentic/work/OP-M03/contracts-v2.md. Tickets mobiles #70/#71 ouverts, conception seule confirmée. Les routes v2 sont absentes.

Socle : Django 5.1.4 / Python 3.13, DRF 3.15.2, drf-spectacular ; main.session_authentication et session_services M02 sont réutilisables. main.urls sépare v1/v2. Nurse.user OneToOne et patients M2M définissent les droits. PatientSerializer v1 __all__ et prescriptions signées ne conviennent pas aux lectures minimales. Tests conftest APIClient historique, tests main sessions et nurse views lus.

Dépendance #267 : quatre champs privés TextField nullable normalisés, query search_normalized lazy SQL. Aucun changement de normalisation normative. La recherche côté Python sur des objets patients est exclue. Les paramètres sont validés avant conversion entière et slice. Requêtes distinct/count et LIMIT/OFFSET SQL.

Risque logs observé : Django request/server écrit chemin complet ; DEBUG peut tracer SQL/query, Sentry send_default_pii=True. Protéger seulement ces lectures via middleware de contexte, redaction des journaux applicatifs et callbacks Sentry ; fournir format Gunicorn sans URI/headers. Aucun proxy/hébergement réel n'est qualifié. Conserver comportements v1 et sessions.

Sécurité : Bearer opaque uniquement ; requête patients restreinte par nurse__user ; aucune création de Nurse ni accès cabinet/admin. Secrets/config production exclus ; tests env synthétique sans .env. Actions externes autorisées : tickets et PR, pas merge/déploiement. Rollback retirer routes v2, conserver v1/shadows.

Gate PASS : modules/tests inspectés, dépendances/fichiers identifiés, risque serializer traité en #267 ; aucune décision produit ouverte. Exécution endpoint sans publication avant #267 vérifié. Aucun SLA prétendu.

Complément confirmé pendant Verify : main/migrations_helpers.py et main.0002 imposent FORCE RLS sans politique sur nurse_nurse_patients. Une connexion PostgreSQL ordinaire ne voit pas ces associations ; aucune disponibilité sous un rôle sans BYPASSRLS n'est prouvée. Tests SQLite et baseline PG superuser restent distincts d'une qualification du rôle cible. Aucun changement de politique RLS ni inspection production ; limite de livraison explicitée dans reads-v2.
