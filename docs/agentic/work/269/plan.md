# Plan — #269

PLAN: PASS. LARGE, complexité élevée auth/données sensibles ; modèle hérité disponible, revues indépendantes puis Lead Tech distinct sur SHA final. Budget non observable.

1. Deux serializers résumé/page fermés. Base PatientPrescriptionRead.initial : auth puis Patient filtré nurse__user, Prescription filtrée patient_id si détail, ID positif validé et aucun test global.
2. List/detail sur PrivateReadView #268 ; query serializers communs, détail serializer vide. HEAD hérite GET ; OPTIONS commun vérifie droits initial et query.
3. Routes str path pour envoyer les ID malformés à l'auth puis404 JSON au lieu404 HTML du resolver. Enregistrer seulement les deux chemins attendus.
4. Tests deux patients A/étranger B, partage de patient autorisé explicite, refus ordonnance autre patient ; pagination45/101, ordre stable, dates inchangées, schemas exacts, storage zéro, permissions HEAD/OPTIONS, query priorités, cache/logs/count SQL. Auth expiré/révoqué/historique, profil absent sans création. Régressions v1/M02, OpenAPI.
5. Environnement #267 runner env-i sans dotenv, make unittest/lint/migrations, HTTP loopback synthétique. Documentation routes et Verify. Reviewer indépendant → commit unique Refs#269 → PR base feat/268-m03-patients → CI verte → Lead Tech indépendante finale. Aucun merge/déploiement.

Worktree269-m03-prescriptions branche feat/269-m03-prescriptions, root seul écrivain ; fast-forward dépendance #268 une fois sa tête finale committée, aucune réécriture historique. Pas de mutation de modèle. Rollback retirer routes v2 avant schéma ; aucun coût/donnée réelle.

Gate PASS : steps/fichiers/AC/tests/dépendances définis, aucune contradiction normative résolue silencieusement. Disponibilité finale dépend vérification #267/#268, pas de faux PASS de livraison.
