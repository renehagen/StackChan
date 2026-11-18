# Validatie

## Firmware v15 en gesproken bediening — 18 november 2026

De USB-upload is geverifieerd. De fysieke StackChan verwerkte de drie akoestische opdrachten. De firmware stelde 65% in, de bridge bevestigde microfoondemping en de stopopdracht beëindigde de sessie met de status **Gesprek gestopt**. Een laatste gesproken opdracht herstelde het oorspronkelijke volume van 70%. Alle 89 regressietests slaagden.

## Firmware v14 en Home Assistant-scripts — 18 november 2026

De USB-upload is geverifieerd. Een gesproken testopdracht op de fysieke StackChan leidde tot één geslaagde scriptoproep via de Assist-bridge. Home Assistant bevestigde via de statussen zowel het starten van het script als het activeren van de bijbehorende scène. Beide audiogolven werden tijdens de proef bijgewerkt. Alle 82 lokale regressietests slaagden.

## Firmware v13 — 18 november 2026

De luistergolf heeft een geleidelijkere niveauschaal. Met dezelfde akoestische testvraag was de hoogste gemeten uitslag 53 van 59 pixels vanaf de middellijn. De StackChan verwerkte de vraag en het antwoord zonder gemeten spraakgat.

## Firmware v12 — 18 november 2026

Tijdens de proef op de geflashte hardware bleef de spreekweergave zichtbaar in de stilte na de hoorbare spraak. Daarna verscheen de luistergolf. Een hosttest controleerde de middenlijn tijdens stilte, de afstand van 10 pixels en de maximale balkverhouding. Het gesprek duurde 28,1 seconden. Tijdens het antwoord pauzeerde de netwerkverwerking 650 ms. Er waren geen bufferoverlopen, audiohardwarefouten of gemeten gaten midden in spraak. De oorzaak van de eerder gemelde hapering bleef onbekend.

## Firmware v11 — 18 november 2026

De USB-upload is geverifieerd. De SHA-256 van de firmware is `55bd9164e607e8ee85addb99af21d083ca1dc5eeabcdb269268a0e712ffcc348`. In een hosttest controleerden we de luisterrichting en de negen vaste balkposities met hoogteverhouding 1–3–1–2–1–2–1–3–1. De test controleerde ook de gecentreerde tekst **Gesprek gestopt**.

De fysieke StackChan voerde een gesprek van 45,3 seconden met twee akoestische testvragen en twee antwoorden. De schermtaak werkte bij beide vragen de luistergolf en bij beide antwoorden de spraakgolf bij. Tijdens het tweede antwoord duurde een gecontroleerde netwerkpauze 650 ms. De metingen registreerden nul bufferoverlopen, nul audiohardwarefouten en nul milliseconden lege afspeelbuffer midden in spraak. Na Stop rapporteerde de robot de gestopte sessie en waren de knoppen weer zichtbaar. Alle 81 regressietests slaagden.

## Firmware v9 en netwerkonderbrekingen

Om 22:15:09 meldde macOS `Clamshell Sleep` op batterij. De Mac werd om 22:15:55 wakker en de bridge zag de StackChan-verbinding wegvallen. Het volgende gesprek eindigde om 22:17:38 met `device_disconnected`. Na het opnieuw verbinden rapporteerde de robot `audio_send_failed`. De netwerkloop had 2.503 ms geblokkeerd. De I2S-tellers voor schrijffouten en leestime-outs waren nul. De precieze oorzaak van de tweede netwerkonderbreking is niet vastgelegd.

De firmware controleert nu eerst of de TCP-verbinding schrijfruimte heeft. Tijdens het wachten kan hij ontvangen audio blijven verwerken. De bridge haalt een grote achterstand in microfoonaudio versneld in. V9 is via USB geflasht en door esptool geverifieerd. De SHA-256 is `f2ea883d9a66b6eb03e1f139e9d50f4aef899511b66647c691e8ffdda85e1cce`. Alle 81 lokale regressietests slaagden, inclusief een controle dat 160 opnameframes na een pauze van 3,2 seconden op volgorde blijven.

De fysieke StackChan voerde met de bijgewerkte bridge één gesprek van 125,1 seconden. De microfoon nam alle drie de synthetische vragen en alle drie de gesproken antwoorden waar. De test hield het zenden van microfoonaudio eenmaal 3,2 seconden tegen en pauzeerde bij een ander antwoord de hele netwerkverwerking 1,55 seconde. De StackChan mat 0 ms lege afspeelbuffer midden in spraak, nul I2S-fouten en nul overlopende opname- en afspeelbuffers. Ook de bridge registreerde nul bufferoverlopen. Stop beëindigde de sessie op verzoek en OpenAI bevestigde de afsluiting. In een eerdere proef met tragere afbouw van de opnameachterstand bleef de sessie actief, maar werd op de tweede vraag geen voldoende lang antwoord gemeten.

## Firmware v8 met een time-out van 10 seconden

Esptool verifieerde de USB-upload. De SHA-256 van de firmware is `e993fd6846e9cf9b93a28740d2443ed04c0e027684b6a00100b1241a4fbf52fc`. De schermtaak las bij het wisselen van zichtbaarheid een pixel binnen elk van de drie knoppen en een achtergrondpixel uit het lcd-geheugen. Bij het begin van een echte GPT Live-sessie waren de drie knoppen zichtbaar. Bij de uitlezing na 10,12 seconden hadden alle drie de knopposities de achtergrondkleur. Zodra het gesprek was afgesloten, waren de drie knoppen terug. Een hosttest van de aanraakcode verifieerde dat een tik op de verborgen Stop-knop alleen de balk toont en dat de balk na nog eens 10 seconden opnieuw verdwijnt.

## Firmware v7 en gesproken bediening van Nano Shapes

De USB-upload is door esptool geverifieerd. De SHA-256 van de firmware is `c17dea5deddf590f6722a78cfe3ef01e4ae15e9da6b0df24a2460f0017066a6c`. Alle 80 Python-regressietests slaagden.

De fysieke StackChan speelde de twee Nederlandse testopdrachten af en nam ze op met zijn eigen microfoon. Ze gingen via de lokale bridge en GPT Live naar Home Assistant. De Home Assistant-historie van de testlamp registreerde om 21:58:04 de overgang van aan naar uit en om 21:58:40 van uit naar aan. Beide bedieningsaanroepen gebruikten de naam `Nano shapes` en ruimte `Woonkamer`. Bij het aanzetten mislukte eerst de zoekopdracht op `Nanoshapes`. Een zoekopdracht in de woonkamer leverde de vrijgegeven naam op.

Een gesproken statusvraag over de onbekende lamp Zebra Zeven leverde de fout `No exposed entities matched name 'Zebra 7'` op. De bridge registreerde de doorgegeven naam en de ruimte `woonkamer`. Na de aanvullende zoekopdracht volgde een gesproken reactie en bleef de bediening uit. De microfoon mat het antwoord bij de luidspreker. De precieze tekst van dat antwoord is niet opgenomen.

Tijdens de twee gesprekken bevestigde USB-diagnostiek het verdwijnen van de knoppen en hun terugkeer na Stop. Een aparte hosttest van de firmwarecode simuleerde aanrakingen met de knoppen verborgen. Zowel een tik op het gezicht als een tik op de positie van Stop toonde de knoppen. De balk verdween na nog eens 30 seconden. Een tweede tik op de zichtbare Stop-knop gaf de stopopdracht door.

## Firmware v6 en akoestische gesprekstest

Bij de gemelde fout op v5 zag de bridge een wifi-verzending van 613 ms, gevolgd door een volle opnamebuffer en de stopreden `capture_overflow`. Tijdens de ontwikkeling van v6 bleef een zelfstandig getest gesprek van 110 seconden actief met drie antwoorden. Een schrijfvertraging van 1.145 ms ging in die tussenversie nog samen met negen verloren opnameblokken op het apparaat en tien in de bridge.

De uiteindelijke v6 is geflasht en gecontroleerd door esptool. De firmware-SHA-256 is `bf8cd980b4d492ec6870b87e89a19ee6265ec08bca60234cd69b4f1d95c7fa1f`. Alle 78 lokale regressietests slaagden, inclusief de controle op het reconstrueren van gebundelde PCM en stilte.

De fysieke StackChan speelde in één echte GPT Live-sessie drie keer een synthetische Nederlandse vraag af. De eigen microfoon leverde de vraag aan de lokale bridge. De test telde terugontvangen spraak tijdens weergave en mat het microfoonsignaal bij de luidspreker. De inhoud van de gesproken antwoorden is niet getranscribeerd.

| Controle op de geflashte v6 | Uitkomst |
|---|---|
| Duur en beurten | 110,5 s, drie vragen en drie antwoorden |
| Teruggekoppelde hoofdgebaren per beurt | 3, 3 en 2, zonder servofout |
| Extra netwerkpauze tijdens het tweede antwoord | 650 ms |
| Overlopen van opname- en afspeelbuffers | 0 op de StackChan en 0 op de bridge |
| I2S-leestime-outs en schrijffouten | 0 |
| Gemeten lege afspeelbuffer midden in spraak | 0 ms |
| Afsluiting | `user_stop` en eindbevestiging van OpenAI |

De herhaalbare test staat in `scripts/test_device_voice.py`. Met `--stall` doorloopt hij ook de gecontroleerde netwerkpauze. Met `--report` bewaart hij de meetwaarden als JSON.

## Firmware v5 met hoofdbewegingen

Firmware v5 is via USB op de officiële StackChan geflasht. Esptool verifieerde de upload. De SHA-256 van het firmwarebestand is `70c158a96fd854c6183bfefad87e3e3be8f7b1ad634307434ff1417bae8513ab`. Bij het opstarten wacht de firmware tot de twee positiesensoren antwoorden. Korte herhaalpogingen bleken op dit apparaat nodig.

De hardwaretest gebruikte dezelfde bewegingscode als een gesproken antwoord. Een stap van de positiesensor komt overeen met 0,3125°. Beide bewegingen werden voltooid en de vrijgave van het motorkoppel werd op beide assen uitgelezen.

| Gebaar | Begin | Uitslag | Terug | Gemeten beweging |
|---|---:|---:|---:|---:|
| Opzij draaien | 684 | 697 | 686 | 4,1° |
| Knikken | 792 | 803 | 793 | 3,4° |

De proef draaide in stand-by. Tijdens de twee bewegingen bleven de tellers voor audio-schrijffouten en leestime-outs op nul. Na de herstart bevestigde de lokale bridge opnieuw de apparaatverbinding.

## Firmware v4 met vernieuwd gezicht

De releasebuild voor de CoreS3 gebruikt 49.704 bytes statisch RAM en 1.135.601 bytes flash. De firmware bevat de twaalf RGB565-afbeeldingen voor ogen, wenkbrauwen, wangen en mond. De visuele controle gebruikte de pixels uit dezelfde beeldgegevens als de firmware. Op de officiële StackChan is de flash via USB geschreven en door esptool op hash gecontroleerd. De SHA-256 van de firmware is `58ed7734df05e46f9e590de1e5c4718aef17f480c85fac94528888686e6e9b25`.

Na herstart meldde de StackChan zich opnieuw aan bij de lokale bridge. Tussen 10,9 en 40,9 seconden na opstarten liep zowel RX als TX met 16.000 samples per seconde. Het gemelde volume was 90%. In deze rustmeting bleven de tellers voor I2S-fouten, leestime-outs en overlopende opname- en afspeelbuffers op nul. Audio draait op core 0 met prioriteit 5 en het scherm op core 1 met prioriteit 1. Een mondbeweging ververst 56 × 34 pixels.

Bij de gesprekstest op de fysieke StackChan bevestigde de gebruiker de weergave en de geluidskwaliteit met “Ronde lijnen en vloeiende spraak”.

## Eerdere verificatie van v3

Firmware v3 is gebouwd en geflasht; uploadhashes zijn gecontroleerd. Wifi en WSS zijn verbonden. De gebruiker bevestigt: “Gesprek, spraak en scherm werken goed”.

| Controle | Resultaat |
|---|---|
| ESP32-S3 releasebuild | Geslaagd |
| Statisch RAM | 49.600 / 327.680 bytes |
| Programmagrootte | 1.065.189 / 6.553.600 bytes |
| Binaire bestandsgrootte | 1.065.552 bytes |
| Tests | 77 geslaagd: 47 bridge, 20 HA, 3 OAuth-aanmelding, 6 configuratie, 1 WSS/TLS |
| Echte gesprekstest | Stop door gebruiker na 90,8 s; afsluiting na 92,6 s bevestigd |
| Bridgefouten / bufferoverflows in gesprekstest | Geen |
| Volume | 90% in firmware en apparaattelemetrie |
| PSRAM | 8.385.703 bytes gerapporteerd |

Firmware SHA-256: `8bca11f6433056c71ad83c72fdea7253c77848cda37b639e7ca4f1c0b13fb18d`

De tests gebruiken lokale mockservers en tijdelijke certificaten. De regressietest voor 300 ms netwerkstilstand gevolgd door vijftien frames tegelijk slaagt; mute en stop behouden voorrang. De oude fatale invoersnelheidscontrole is vervangen door begrensde buffering en verzending op audiosnelheid.

## Tools

Webzoeken en lokale Assist-tools zijn toegevoegd aan de bridge. Een echte OpenAI Live-test via dezelfde dispatcher slaagde: één voltooide webzoekopdracht, één geslaagde Home Assistant-statusaanroep, nul tool-/backendfouten en bevestigde sessieafsluiting. Hierbij zijn uitsluitend stilteframes en een testvraag gebruikt; er is geen apparaat bediend.

De Home Assistant 2026.8.1-installatie is via OAuth gekoppeld. De native MCP-integratie is met alleen Assist via de normale configuratieflow toegevoegd en staat geladen; twaalf ondersteunde functies zijn gevonden. Er zijn geen YAML-bestanden, apparaatvrijgaven of andere MCP-serverrechten gewijzigd; Home Assistant is niet herstart. De lokale OAuth-aanmelding is opgeslagen met rechten 0600. De achtergrondservice meldde aanvankelijk `ha_unreachable`/OS-fout 65, terwijl dezelfde verbinding vanuit Codex slaagde. Nadat de gebruiker Python toestemming gaf voor lokaal netwerk, is de bridge herstart: `home_assistant_ready` met twaalf tools en een nieuwe apparaatverbinding zijn bevestigd. Daarna bevestigde de gebruiker op de vraag naar de gesproken webzoek- en Home Assistant-opdrachten: “Beide werken, ook apparaatbediening”. Daarmee is ook de praktische bediening via de StackChan door de gebruiker bevestigd.

Tijdens de echte gesprekstest registreerde het apparaat nog korte verzendvertragingen tot 317 ms, 17 verloren opnameframes en 10 verloren weergaveframes. Het gesprek bleef actief; de gebruiker meldde goede spraak en een rustig scherm. Dit bewijst geen verliesvrije of langdurig stabiele verbinding. De oorzaak van de resterende transportpauzes is niet vastgesteld; diagnostiek blijft actief. Afsluiten verwijdert resterende gebufferde audio en telt die afzonderlijk als drops.

Een latere sessie stopte na 99,3 seconden door `playback_overflow`: 29 nieuwe verloren uitvoerframes overschreden de firmwaregrens van 25 in vijf seconden. RX/TX liepen rond 16.000 samples/s zonder I2S-fouten. De metingen passen bij een transportpauze gevolgd door een ontvangstburst; de precieze netwerkoorzaak is onbekend. De tooluitbreiding verandert deze firmwarebewaking niet.

## Firmware en bediening

RX/TX draaien met hogere prioriteit dan netwerk en scherm. Het scherm wordt uit een PSRAM-framebuffer getekend. De afspeelbuffer is 640 ms; de bridge buffert maximaal 500 ms invoer en twee seconden uitvoer. Volume is instelbaar in stappen van 10%, standaard 90%, en wordt na het gesprek bewaard. Er is geen AEC: wacht op “Ik luister”; tijdens robotspraak wordt microfoongeluid vervangen door stilte.

De officiële StackChan wordt herkend als board 27. De audiodriver initialiseert zijn interne I2C-bus expliciet op SDA12/SCL11. De schermverlichting wordt na audio-initialisatie aangezet op helderheid 160. Servo’s en camera zijn niet geïnitialiseerd.

## Lokale installatie en herstel

Wifi en bridge zijn geprovisioneerd en blijven bewaard bij firmware-upload. Een LaunchAgent start de bridge bij inloggen; de Mac moet wakker en bereikbaar blijven. De API-sleutel staat uitsluitend lokaal in `.env.local` met rechten 0600 en heeft een looptijd van 30 dagen.

De oorspronkelijke 16 MB flash is bewaard als `.local/backups/stackchan-before-2026-11-17.bin`, met SHA-256-manifest en rechten 0600. Deze kopie kan credentials bevatten en is uitgesloten van Git. Het tijdelijke wifi-herstelbestand is verwijderd.

Veilige JSON-diagnostiek staat in `.local/logs/bridge-error.log`; audio, transcripties en sleutels worden niet opgeslagen. Het apparaat levert elke vijf seconden statistieken en de bridge elke vijftien seconden sessietellers plus eerste stopreden. USB `STATUS` en `DIAG` blijven beschikbaar, maar een USB-open kan reset veroorzaken en wordt tijdens gesprekken vermeden.
