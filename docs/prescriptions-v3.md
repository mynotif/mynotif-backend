# Lectures v3 des ordonnances et documents

Ces routes sont privées et Bearer B01 uniquement. Le serveur réévalue le lien
Nurse → patient à chaque requête. Patient, ordonnance et pièce hors périmètre
répondent tous `404 {code:"not_found",message,fields:{}}`. Staff et superuser
n'ajoutent aucun droit. Toutes les réponses portent
`Cache-Control: private, no-store`, `Pragma: no-cache`, sans ETag. Les binaires
portent `X-Content-Type-Options: nosniff`, un Content-Type vérifié et un nom
attachment fixe sans donnée patient. Aucun lien de stockage n'est renvoyé.

| Méthode | Route | Réponse |
|---|---|---|
| GET | `/api/v3/patients/{patient_id}/prescriptions/` | Page de résumés |
| GET | `/api/v3/patients/{patient_id}/prescriptions/{prescription_id}/` | Résumé |
| GET | `/api/v3/patients/{patient_id}/prescriptions/{prescription_id}/documents/` | Manifeste courant |
| GET | `/api/v3/patients/{patient_id}/prescriptions/{prescription_id}/documents/{asset_id}/content/` | Octets de l'original ou du dérivé |

Les routes acceptent aussi HEAD et OPTIONS avec la même authentification et le
même contrôle objet ; HEAD n'inclut aucun corps et n'accède pas au stockage.
Pour une pièce historique, HEAD n'interroge pas le stockage pour connaître la
taille : Django émet `Content-Length: 0` pour son corps vide. Son Content-Type
suit l'extension connue ; GET vérifie le type par signature.
Les assets v3 utilisent les métadonnées validées et renvoient les mêmes en-têtes
de représentation que GET.
OPTIONS renvoie `{"allowed_methods":"GET, HEAD, OPTIONS"}` après le contrôle
de portée. Les autres méthodes répondent 405. OpenAPI publie les opérations GET;
HEAD et OPTIONS sont exercées par les tests HTTP.

Les résumés ont exactement cette forme ; aucune échéance calculée, URL ou
information email n'est exposée :

```json
{"id":1,"patient_id":2,"prescribing_doctor":"Dr Exemple","start_date":null,"end_date":null}
```

La liste accepte uniquement `page` (défaut 1) et `page_size` (défaut 20,
maximum 100), une seule occurrence de chacun. Le tri est `end_date DESC NULLS
LAST, id DESC`. Le compte et la pagination incluent prescriptions historiques
et v3 avant découpage.

Le manifeste fermé contient `version`, `assets`, `pages`. Chaque asset est
`{asset_id,mime_type,byte_size,page_count,sha256,role,original_index,source_asset_id}`.
Chaque page est
`{source_asset_id,source_page,display_asset_id,display_page,rotation,crop}`.
Les IDs sont opaques et la route content accepte uniquement un asset appartenant
à la dernière version confirmée de cette ordonnance. Les octets originaux sont
immutables ; les transformations sont des assets dérivés distincts. Le client
applique la recette indiquée uniquement à l'aperçu de l'original ; pour un
dérivé, les transformations sont déjà appliquées.

Pour une ordonnance historique sans version documentaire v3, le serveur expose
`photo_prescription` comme un asset original synthétique avec UUIDv5 déterministe
de l'ID d'ordonnance, numéro de version 1, une page, rotation 0 et aucun crop.
Il ne crée ni copie ni ligne Asset. Le type est détecté par signature et le
manifeste calcule taille et SHA-256 depuis une lecture privée bornée à 20 MiB.
Un objet absent, illisible, trop grand ou d'un format non pris en charge est
indisponible ; le fichier historique n'est jamais modifié.

La table ci-dessus est l'interface HTTP publique. OpenAPI décrit les opérations
GET et leurs schémas fermés ; HEAD/OPTIONS sont des méthodes utilitaires sans
opération métier exposée.
