# Examples — output di esempio

File JSON con l'output reale di alcuni turni dell'advisor (il "widget JSON" restituito dal bridge),
catturati durante i test sulla **Snap4City JupyterHub**. Servono come esempi di test / dati di
input-output per la consegna (istruzione: "esempi di test", "file dati input e output").

## Come produrli

Ogni turno sovrascrive `outputs.txt` (nella cwd del bridge, `.gitignore`d) con il JSON completo del
turno. Eseguire i test descritti in `README.md` §3, poi **copiare** il contenuto di `outputs.txt`
in un file qui con il nome corrispondente:

| File | Scenario | Query di esempio |
|---|---|---|
| `route-foot.json` | Rotta a piedi (modo singolo) | *"da Piazza del Duomo a Santa Croce a piedi"* |
| `route-tre-modi.json` | Tre modi in un turno | *"da Piazza del Duomo a Santa Croce"* |
| `route-bus.json` | Trasporto pubblico con leg | *"da Piazza del Duomo a Santa Croce in autobus"* |
| `near-farmacia.json` | Categoria vicino al GPS | *"portami alla farmacia più vicina"* |

## Formato

La forma del payload è documentata in `README.md` → "The widget payload":
`status`, `request_type`, `data` (`wkt`, `distance_km`, `duration`, `mode`, `routes[]`, per il bus
anche `legs[]`) e `messages[]` (ultimo turno `assistant` = testo della risposta).

## Note

- Il widget JSON non contiene segreti (solo coordinate geografiche pubbliche): nessuna sanitizzazione
  necessaria oltre a non includere `job_id` / `stage` (che comunque vivono solo nel livello di
  trasporto e non entrano nel payload).
