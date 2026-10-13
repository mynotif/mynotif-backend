# Design — #269

DESIGN: PASS. Deux GET imbriqués, même résumé fermé : id/patient_id/prescribing_doctor/start_date/end_date exactement. Pas de fichier/URL/email/statut calculé, aucune correction dates source. Liste enveloppe commune, page défaut1 jusqu'à2147483647/page_size défaut20 max100, tri end_date DESC/id DESC ; détail aucune query. GET/HEAD/OPTIONS seuls, mutation route valide 405 après auth.

Ordre : M02 Bearer uniquement → patient associé au Nurse courant → ordonnance appartenant à ce patient → query. IDs positifs sinon 404 générique après auth, zéro scan existence étranger. Absence Nurse 404, même pour staff. OPTIONS effectue droits puis query et annonce méthodes sans métier. HEAD exécute même chemin que GET sans corps. Erreurs et headers privés communs #268, aucun ETag/304. Logs gabarits sans IDs/query/jeton/corps ; infra réelle non qualifiée.

Dates passées/inversées reproduites sans calcul clinique. État vide 200, page hors plage404, query invalide400 après droits. Frontend doit adapter puis tester Android, aucun backend test ne valide navigation après second401. Gate PASS : tous AC normatifs représentés.
