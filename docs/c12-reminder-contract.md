# C12 — Contrat métier des dates et rappels d’ordonnances

## Statut et portée

Contrat métier stabilisé adopté comme référence normative du dépôt backend par
la révision documentaire proposée pour [OP-C12 #229](https://github.com/mynotif/mynotif-backend/issues/229).
L’adoption est livrée à la fusion de cette révision ; elle ne démontre pas que
le code existant implémente ces règles. Règles initiales approuvées le 14 octobre
2026, clarifications du Product Lead le 18 octobre 2026. La revue indépendante
**documentaire de la source** est PASS. Le Product Lead rapporte la validation
métier IDEL acquise ; identité et date de cette validation ne sont pas fournies
dans le dossier. Aucun visa nominatif ni preuve runtime n’est déduit de ce rapport.

Ce contrat fixe les règles métier et T01–T25. Il n’ajoute aucun endpoint, champ,
migration ou comportement API ; les contrats fermés de lectures
[v2](prescriptions-v2.md), [v3](prescriptions-v3.md) et d’[import v3](document-import-v3.md)
restent applicables. Aucune activation de fournisseur, donnée de santé réelle,
OCR, exécution B06, qualification production ou déploiement n’est inclus.

Le backend est autoritatif. Une date d’ordonnance est une date civile
`YYYY-MM-DD`, jamais un instant à convertir selon le fuseau du téléphone. La
validité calendaire ne constitue ni une autorisation ni une interdiction de
soin. Le jugement médical et administratif reste aux professionnels concernés.

## Règles métier normatives

### Dates et état de validité

- Référence temporelle : date civile de `Europe/Paris`.
- Pour les dates connues `début = S`, `fin = E` et le jour parisien `D`,
  l’intervalle est valide si `S ≤ D ≤ E`. Les deux bornes sont inclusives ;
  `E` est le dernier jour de validité selon les dates enregistrées.
- `S > E` est invalide et doit être rejeté à toute entrée/modification. Aucune
  normalisation ni programmation de rappel ne s’applique à cet intervalle.
- Une date inconnue reste `null`. Aucune date de remplacement, date du jour,
  durée par défaut ou expiration ne peut être inventée.
- Toute date requise inconnue : aucun rappel d’échéance n’est programmable ;
  conserver « Date à compléter » et `null`, sans date de remplacement.
- Début futur : l’ordonnance est « À venir » ; aucun rappel ne part avant le
  début de validité. À partir de S, un jalon devenu éligible peut être traité
  dans la fenêtre du jour seulement ; passé 18:00, il est manqué et n’est pas
  reporté au lendemain.
- Après `E`, statut d’affichage « Ordonnance expirée — vérification nécessaire ».
  Ce statut n’autorise ni n’interdit des soins et ne déclenche pas de rappel
  produit additionnel.

### Fuseau et exécution

- Calculer tous les jours J-n en jours calendaires `Europe/Paris`, quelle que
  soit la zone où les dates sont saisies ou consultées.
- Traiter les opportunités entre 09:00 inclus et 18:00 exclu, heure murale de
  Paris, tous les jours, week-ends et jours fériés inclus. Le changement
  été/hiver ne décale pas ces bornes locales.
- Une date-only reçue depuis un autre fuseau conserve exactement son année,
  mois et jour ; ne pas la transformer en date voisine par conversion d’instant.

### Jalon produit et préférence IDEL

- Première pré-alerte : l’IDEL choisit un délai de J-7, J-8, J-9 ou J-10 ;
  J-10 est le défaut. Elle constitue une seule alerte pour cette échéance,
  jamais quatre alertes quotidiennes.
- Relance complémentaire unique à J-1 si l’état de suivi n’est pas
  « Renouvelée ». Aucun autre rappel automatique produit.
- Le réglage IDEL modifié s’applique immédiatement aux échéances actives
  existantes et aux nouvelles. Recalculer les alertes futures ; ne jamais
  renvoyer une notification déjà émise pour la même échéance et le même type
  d’alerte.
- États métier de suivi IDEL : « À traiter », « Démarche en cours »,
  « Renouvelée ». Seul « Renouvelée », après import et vérification de la
  nouvelle ordonnance, arrête la relance. Clic, acceptation fournisseur,
  réception/lecture push ou « Demande envoyée » ne valent pas confirmation.

### Retard, indisponibilité et changements

- Une opportunité manquée peut être rattrapée uniquement le même jour civil
  `Europe/Paris`, dans la fenêtre 09:00–18:00. À 18:00, elle est marquée
  manquée/expirée et n’est pas reportée au lendemain, même si `D≤E`.
- Un jalon prévu aujourd’hui, notamment J-1, reste évalué normalement ; les
  jalons d’un jour antérieur ne sont pas rejoués ou regroupés avec lui.
- Regrouper les opportunités actuellement éligibles du même IDEL et du même
  jour de service dans un rappel générique, sans donnée clinique dans le push.
  L’application recharge les échéances autorisées. Un jalon manqué d’un jour
  antérieur n’est pas réintroduit dans un regroupement ultérieur. La règle pour
  une nouvelle échéance éligible après un premier envoi le même jour ne produit
  pas de second rappel générique ce jour-là ; attendre le prochain jalon
  planifié. Décision du Product Lead le 18 octobre 2026. Tracer séparément les
  tentatives techniques.
- Après l’échéance, aucune ancienne pré-alerte n’est envoyée. Afficher le
  statut expiré défini plus haut.
- Une modification de l’échéance invalide les alertes futures de l’ancienne
  version et recalcule les alertes futures sur la nouvelle échéance.
- Une annulation annule les rappels futurs et conserve l’historique.
- Une nouvelle ordonnance ne remplace jamais automatiquement l’ancienne.
  Après import et vérification, l’IDEL confirme explicitement le remplacement ;
  conserver un lien de traçabilité et recalculer les rappels sur la nouvelle
  échéance confirmée. L’ancienne n’est alors plus rappelée.

### Fiabilité, historique et sécurité métier

- Les notifications sont idempotentes : une échéance et un type d’alerte
  donnés ne produisent pas de notification en double, y compris après
  concurrence, redémarrage ou réponse de transport perdue.
- Historiser les rappels et leurs transitions, avec états `planifié`, `envoyé`,
  `échec`, `annulé`, et les tentatives d’envoi nécessaires au diagnostic.
  Les logs techniques ne contiennent ni identité patient, ordonnance, date de
  soin, token ni texte clinique (ADR-008).
- Le résultat d’une soumission sans réponse est incertain ; il ne vaut ni
  échec certain, ni preuve de livraison, ni renouvellement. Le système doit
  résoudre l’incertitude sans générer de doublon visible.
- Avant chaque tentative, revalider échéance/version, annulation,
  remplacement, confirmation, préférence, droits et autorisation du tenant.
  Push générique ; au clic, session requise et dossier rechargé avec droits.
- Ne jamais déduire du statut expiré une interdiction ou autorisation de soins.

## Critères d’acceptation C12

1. Les mêmes dates-only produisent la même date civile indépendamment du
   fuseau de saisie/appareil. J-n est calculé dans `Europe/Paris`.
2. `S=D`, `D=E` sont valides ; `D<S` et `D>E` ne le sont pas. `S>E` est
   rejeté. Toute date inconnue reste nulle sans valeur dérivée.
3. Toute date requise inconnue ne crée aucun rappel et reste à compléter ;
   début futur affiche « À venir » et aucun jalon ne part avant le début.
4. À une échéance E, produire au plus une première alerte au délai choisi
   (J-7…J-10, J-10 défaut), puis au plus une J-1 si non renouvelée ; aucun
   autre rappel automatique.
5. Traiter les opportunités dans la fenêtre 09:00 incluse–18:00 exclue
   `Europe/Paris`, chaque jour civil, y compris week-ends et jours fériés.
6. Rattraper un jalon manqué uniquement le même jour avant 18:00 ; aucun report
   au lendemain. Une opportunité J-1 du jour reste évaluée normalement, sans
   rejouer un jalon antérieur ; après E, afficher le statut expiré sans alerte.
7. Modification invalide les anciens jalons futurs ; annulation annule les
   futurs sans effacer l’historique ; remplacement exige confirmation IDEL
   après import/vérification, crée le lien et transfère le suivi.
8. Changer la préférence recalcule immédiatement les seules alertes futures
   sur les échéances actives et ne réémet aucun couple échéance/type déjà
   envoyé.
9. Les états IDEL sont `À traiter`, `Démarche en cours`, `Renouvelée` ; seuls
   import, vérification et confirmation humaines autorisent le dernier état.
10. Appels concurrents, redémarrages et réponse perdue ne créent aucun doublon
    visible ; historique conservé avec état et tentatives sans donnée clinique.
11. L’état « expirée » n’est jamais présenté comme décision de soin ; droits
    serveur revérifiés et payload push générique.

12. Regrouper les opportunités éligibles par IDEL et jour dans un rappel
    générique ; après le premier envoi du jour, aucune deuxième notification
    générique, même si une nouvelle échéance devient éligible. Attendre le
    prochain jalon planifié.

## Scénarios de tests prévus (synthétiques, pas exécutés dans C12)

| ID  | Entrée/événement                                                                                           | Résultat attendu                                                                                                                              |
| --- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| T01 | `S=D`, `E>D`                                                                                               | Valide ; aucun décalage de borne.                                                                                                             |
| T02 | `D=E` puis lendemain                                                                                       | Valide le jour E, expirée le lendemain ; statut de vérification.                                                                              |
| T03 | `S>E` via chaque voie d’écriture                                                                           | Rejet uniforme ; aucun rappel créé.                                                                                                           |
| T04 | `S=null`, `E` connue                                                                                       | « Date à compléter » ; aucun rappel ; `null` reste inconnu.                                                                                   |
| T05 | `E=null`, `S` connu ou non                                                                                 | Aucun rappel ; `null` reste inconnu.                                                                                                          |
| T06 | `S>D`, E connue, J-10 passé                                                                                | « À venir » et zéro alerte avant S ; à partir de S, rattraper le jalon uniquement dans la fenêtre du jour 09:00–18:00 Paris.                  |
| T07 | ISO date-only saisie en fuseau UTC puis consultée en Paris                                                 | Année/mois/jour identiques ; aucun glissement au jour précédent/suivant.                                                                      |
| T08 | Passage veille du changement CET/CEST puis inverse                                                         | Heure murale 09:00 Paris conservée ; jour civil J-n stable.                                                                                   |
| T09 | Chaque préférence J-7/J-8/J-9/J-10, plus préférence absente                                                | Un premier rappel au jour choisi ; défaut J-10 ; aucune répétition quotidienne.                                                               |
| T10 | Non renouvelée à J-1, puis renouvelée avant tentative                                                      | Une seule J-1 si non confirmée ; après confirmation, tentative supprimée.                                                                     |
| T11 | Service indisponible après pré-alerte, retour le même jour avant 18:00                                     | Rattraper une fois le jalon du jour ; retour après 18:00 ou le lendemain ne le rejoue pas.                                                    |
| T12 | Service revient à J-1 après une première alerte manquée un jour antérieur                                  | N’envoyer que le jalon J-1 du jour ; ne pas rejouer/regrouper l’ancien jalon. Historiser distinctement.                                       |
| T13 | Service revient après E ou après la fenêtre du jalon                                                       | Aucun jalon ancien rejoué ; statut expiré si `D>E`, historique conservé.                                                                      |
| T14 | Modifier échéance avant alertes futures                                                                    | Ancienne version annulée ; nouveau calendrier recalculé.                                                                                      |
| T15 | Annuler avant envoi, puis inspecter historique                                                             | Futurs annulés, événements/tentatives historiques conservés.                                                                                  |
| T16 | Importer ordonnance nouvelle sans confirmer remplacement                                                   | Ancienne reste suivie ; aucune relation ni arrêt implicite.                                                                                   |
| T17 | IDEL vérifie puis confirme remplacement                                                                    | Lien ancien/nouveau tracé ; rappels futurs basés sur nouvelle échéance.                                                                       |
| T18 | Changer préférence avec plusieurs échéances actives                                                        | Recalcul immédiat des futurs ; alertes émises non répétées.                                                                                   |
| T19 | Deux workers concurrents sur même échéance/type                                                            | Une seule notification visible ; état/historique cohérent.                                                                                    |
| T20 | Redémarrage après tentative, réponse fournisseur perdue dans la fenêtre du jour                            | Reprendre la même opportunité sans doublon visible ; à 18:00, aucun nouvel essai métier le lendemain.                                         |
| T21 | Échec fournisseur puis retry borné                                                                         | Réessayer seulement avant 18:00 le jour de l’opportunité ; après la borne, arrêter et historiser sans donnée clinique.                        |
| T22 | Compte/droits révoqués avant tentative ou clic                                                             | Aucun envoi/dossier non autorisé ; état technique conforme à la politique d’accès.                                                            |
| T23 | Ordonnance expirée                                                                                         | Affiche « Ordonnance expirée — vérification nécessaire » sans conseil d’autorisation/interdiction de soins.                                   |
| T24 | Service revient le jour E avant 18:00, puis repasse le jour E+1                                            | E reste le dernier jour valide inclus ; ne traiter qu’une opportunité du jour E dans sa fenêtre. À E+1, aucune ancienne alerte n’est envoyée. |
| T25 | Un rappel générique a été envoyé aujourd’hui ; une nouvelle échéance devient ensuite éligible le même jour | Aucun deuxième rappel générique ce jour-là ; attendre le prochain jalon planifié.                                                             |

Précision T06 : S est le jour où le jalon devient éligible si le début futur
empêchait son traitement. Le rattrapage est permis ce jour-là seulement avant
18:00 Paris ; une opportunité manquée un jour civil antérieur n’est jamais
rejouée à un passage ultérieur. T24 ne crée aucun jalon supplémentaire à E.

Ces scénarios deviennent tests backend/API et tests d’intégration ciblés sous
B06/B08/I01, avec cas appareils/transport C05 et parcours d’affichage M05/M08.
C12 ne les exécute pas et ne constitue pas une preuve runtime.

## Écarts avec le backend existant

Inspection du code versionné à `27a5811219f993cb7561b7700959763ea0c14ad9` ;
ces constats ne décrivent pas la configuration déployée.

- Les [dates du modèle](../src/nurse/models.py) sont civiles et nullables ; les
  bornes de `is_valid()` sont inclusives, mais le calcul utilise
  `datetime.now().date()`. La sélection `expiring_soon()` est J0…J+7 et ne
  vérifie ni début, ni préférence, ni confirmation de renouvellement. Elle
  contredit la cadence et l’éligibilité ci-dessus.
- Le [serializer historique](../src/nurse/serializers.py) ne garantit pas de
  validation croisée uniforme des dates. L’[import v3](../src/nurse/document_import_schemas.py)
  conserve les dates ISO/null et rejette les intervalles inversés connus.
- Le [sender historique](../src/nurse/management/commands/_notifications.py)
  envoie un message générique aux abonnements distincts, sans registre durable
  de jalon ni idempotence démontrée dans cette voie. Le [cron versionné](../terraform/cloudwatch.tf)
  est quotidien à 08:00 UTC, soit 09:00 Paris en hiver et 10:00 en été,
  dans la fenêtre autorisée. Il ne garantit pas 09:00 murale stable ni
  un rattrapage intrajournalier.
- La [confirmation d’import v3](../src/nurse/document_import_views.py) exige la
  relecture patient/pages/dates et crée une ordonnance. Elle ne confirme pas
  un renouvellement et ne crée aucun lien de remplacement vers l’ancienne.
  Ne pas assimiler ses booléens de confirmation à cette décision métier.

Ces écarts sont explicités pour l’adoption du contrat, sans modification de code
ni extension implicite des payloads fermés existants.

## Réalisation séparée et preuves requises

[OP-B06 #236](https://github.com/mynotif/mynotif-backend/issues/236) reste une
story séparée. Ses dépendances **B01/C05**, son DoR, son contrat technique et
ses preuves propres restent requis. L’adoption C12 ne commence pas son Execute
et ne valide pas automatiquement une dépendance. L’ancien critère de comparaison
des cadences dans B06 doit être lu au regard de la cadence métier approuvée ici ;
C12 ne rouvre pas ce choix. Le ticket #236 n’est pas modifié dans ce lot.

La direction des ADR-006/008 source (outbox, idempotence, retries bornés,
revalidation et minimisation) ne prouve pas l’absence de doublon visible après
acceptation fournisseur et perte de réponse. L’exigence métier reste entière.
C05/B08 doivent établir déduplication et réconciliation fournisseur ; B06/I01
doivent concevoir puis démontrer atomicité, concurrence, reprise et persistance.
La correspondance des états métier `planifié/envoyé/échec/annulé` avec les états
techniques `tenté/accepté` reste à définir sans assimiler `envoyé` à reçu/lu.
Les détails de stockage, identifiants, TTL et interfaces ne sont pas décidés ici.

Si la garantie de zéro doublon visible ne peut être soutenue, bloquer le chemin
concerné et documenter l’écart technique ; ne pas affaiblir la règle métier.
Les preuves fournisseur requises avant Execute, le design technique propre,
les Verify synthétiques après implémentation et les gates C04/C05/I01
applicables restent à obtenir. Les journaux restent minimisés et sans donnée
sensible. Les cas appareils/transport C05 et parcours M05/M08 gardent leur portée.

## Provenance de la révision

Source de référence : `mynotif/ordopro_mobile`, copie locale non committée du
worktree `229-c12-contract`, dossier `docs/agentic/work/OP-C12/`, consultée le
18 octobre 2026. Les empreintes ci-dessous identifient les octets consultés ;
elles ne constituent pas un lien vers une révision mobile publiée. Research,
Design, Plan et Review documentent la provenance et les limites, sans être
republiés ici. Le contrat et les clarifications finales du mémo du 18 octobre
priment sur leurs formulations historiques incompatibles : aucun rattrapage
au lendemain, aucune fusion d’un ancien jalon avec J-1 et aucune validation
IDEL présentée comme encore à obtenir dans C12.

| Source locale mobile                            | SHA-256                                                            |
| ----------------------------------------------- | ------------------------------------------------------------------ |
| `docs/agentic/work/OP-C12/contract-candidat.md` | `89821902fd8e3708bc4753b7dcf49668e95a79003f46e58e280b14a5690815b5` |
| `docs/agentic/work/OP-C12/research.md`          | `b72ca14aa479a4b6e04ce61069754fcdc8ac8f444da6619ba77372a575fad2ec` |
| `docs/agentic/work/OP-C12/design.md`            | `4ba7cbad3266edc84f40c043c6d632de23b7a85aebdb3c3294fedafa5552df56` |
| `docs/agentic/work/OP-C12/plan.md`              | `1c5cfe4fbe5de7c17cdb1aefc50718096501e47aec5d5c6bc36689b10114734b` |
| `docs/agentic/work/OP-C12/review.md`            | `44c1d5bf756c7baa37c0103e80d9f8a3d4af154e1858a987bf8f6a00a14fac96` |
| `docs/product/DECISIONS_PRODUIT_2026-10-11.md`  | `8ac22dabd740e719b4bf211333a8662a1f72c23dd4f945cc30f93c471fe133b6` |
