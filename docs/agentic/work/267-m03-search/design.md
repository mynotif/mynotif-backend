# Design — backend #267

PASS. Utilisateur a confirmé explicitement lock_timeout PostgreSQL 2 s.
Quatre TextField nullable editable=False : search_firstname, search_lastname,
search_city, search_phone. Aucun changement des réponses v1. Nouveau helper lazy
`Patient.objects.search_normalized(search)` pour la route #268; validation de
query reste responsabilité du serializer de cette route.

save et update de chaînes maintiennent les champs dérivés; update d'expressions
sur sources est explicitement refusé. bulk_create/bulk_update maintiennent les
champs et refusent les expressions sources. Écriture directe des champs dérivés
refusée via update/update_fields/bulk fields; save recalculera les sources écrites.
Les écritures SQL brutes sont hors garantie ORM.

0014 ajoute seulement les colonnes nullable sans défaut avec lock_timeout
PostgreSQL confirmé (2s proposé). 0015 non atomique remplit les dérivés par lots
keyset, modèles historiques, normalisation figée, update conditionnée aux sources.
Une commande repair_patient_search permet réparation/recontrôle après retrait
anciens écrivains, sans afficher identifiants ni données. Ne pas activer v2 avant
son succès; la migration initiale seule ne prouve pas cette condition.
Rollback : retirer routes v2 d'abord, conserver les colonnes pour compatibilité.
