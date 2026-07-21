# Examples — output reali dell'advisor

File JSON con l'output completo ("widget JSON") di alcuni turni reali dell'advisor,
catturati durante i test sulla **Snap4City JupyterHub**. Servono come esempi di test e
come file di dati input/output per la consegna.

Ogni turno scrive `outputs.txt` (nella cwd del bridge, `.gitignore`d) con il JSON completo;
questi file sono copie di quei risultati.

## File presenti

| File | Scenario | Contenuto principale |
|---|---|---|
| `route-tre-modi.json` | Turno a tre modi (nessun modo indicato) | tre rotte sullo stesso tragitto — auto 2,872 km / 0:04:07, trasporto pubblico 2,715 km / 0:16:59, a piedi 2,010 km / 0:24:07 — ciascuna con la propria geometria e il proprio `detail` |
| `route-bus.json` | Trasporto pubblico, tragitto più lungo | rotta bus 6,936 km / 0:32:10 con i `legs` walk/ride separati (geometria per tratta e fermate di salita/discesa) |
| `near-servizi.json` | Auto + servizi lungo il percorso | rotta in auto 40,478 km / 0:33:58 con l'elenco dei servizi campionati lungo il tragitto, ordinati per distanza dall'ancora |

## Formato

La forma del payload è documentata in `README.md` → "The widget payload": `status`,
`request_type`, `data` (`wkt`, `distance_km`, `duration`, `mode`, `routes[]`, e per il bus
anche `legs[]`) e `messages[]` (l'ultimo turno `assistant` è il testo della risposta).

I file non contengono segreti: solo coordinate geografiche e nomi di servizi pubblici.
`job_id` e `stage` non compaiono perché vivono solo nel livello di trasporto del bridge e
non entrano mai nel payload.
