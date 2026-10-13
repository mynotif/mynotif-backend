# Research — #269 prescriptions v2

RESEARCH: PASS. EXISTING / LARGE, phases produit hors périmètre ; conception normative mobile #73 confirmée, aucune disponibilité présumée. Base 987eab9, dépend #267 puis #268 pour infrastructure lectures. Contrat complet lu, blob 821310cbe0da90aa58a181a6c98cf2458ba13e3f, https://github.com/mynotif/ordopro_mobile/blob/main/docs/agentic/work/OP-M03/contracts-v2.md ; #70/#71/#10 restent ouverts.

Prescription.patient FK obligatoire, dates source sans invariants cliniques. Nurse→Patient M2M définit autorisation ; PrescriptionSerializer v1 calcule is_valid/expiring_soon et URL signée, donc inutilisable en v2. Views/querysets/serializers/urls/tests v1 lus ; ne pas les changer. Lire uniquement les six colonnes du résumé. Aucun stockage ni S3 requis. Tests synthétiques APIClient, M02 réutilisable. Risque principal droits du patient puis ordonnance avant query, notamment OPTIONS/HEAD et IDs invalides ; erreurs et cache communs #268.

Fichiers nouveaux : read_prescription_serializers.py, read_prescription_views.py, tests/nurse/test_read_prescriptions_v2.py ; modifier read_urls.py. Aucun modèle ni migration en #269. Rollback routes v2, v1 intact. Aucun secret, donnée réelle, mutation distante ni navigation Android. Gate PASS : code inspecté, dépendances stabilisées, cas limites et tests identifiés.
