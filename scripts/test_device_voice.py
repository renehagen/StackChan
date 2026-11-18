"""Exercise one real, bounded conversation over USB with a physical StackChan.
The speaker plays the synthetic question. The device microphone is the only audio input.
"""
import argparse
import base64
import json
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_LOG = ROOT / ".local/logs/bridge-error.log"
QUESTION = "Vertel me in drie zinnen iets over de maan."


def prompt(path):
    with wave.open(str(path), "rb") as f:
        if (f.getnchannels(), f.getsampwidth(), f.getframerate()) != (1, 2, 16000):
            raise ValueError("Testvraag vereist PCM16 mono op 16 kHz")
        if not 8000 <= f.getnframes() <= 96000:
            raise ValueError("Testvraag moet 0,5 tot 6 seconden duren")
        return f.readframes(f.getnframes())


class Device:
    def __init__(self, port):
        self.usb = serial.Serial(timeout=.25, write_timeout=2)
        self.usb.port, self.usb.baudrate = port, 115200
        self.usb.dtr = self.usb.rts = False

    def send(self, command, prefix):
        self.usb.write(command + b"\n")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            line = self.usb.readline().strip()
            if line.startswith(prefix):
                return line[len(prefix):]
        raise TimeoutError("Geen USB-bevestiging")

    def command(self, command):
        if self.send(command, b"TEST ") != b"OK":
            raise RuntimeError("Testopdracht geweigerd: " + command.split(b' ')[0].decode())

    def diag(self):
        return json.loads(self.send(b"DIAG", b"DIAG "))


def wait(device, seconds, predicate, *, in_session=False):
    deadline = time.monotonic() + seconds
    d = {}
    while time.monotonic() < deadline:
        try:
            d = device.diag()
        except (TimeoutError, json.JSONDecodeError):
            continue
        if in_session and not d["active"]:
            raise RuntimeError("Gesprek onverwacht afgesloten: " + d["last_stop"])
        if predicate(d):
            return d
        time.sleep(.6)
    fields = ("state", "last_stop", "bridge_connected", "paired_pcm", "head", "voice_test", "net_output_frames")
    raise TimeoutError("Verwachte apparaatstatus bleef uit: " + str({k: d.get(k) for k in fields}))


def run(args, pcm):
    dev = Device(args.port)
    offset = BRIDGE_LOG.stat().st_size
    turns = []
    with dev.usb:
        # Opening USB may reset this board. Allow its bootloader to finish.
        time.sleep(2)
        d = wait(dev, 48, lambda d: d.get("bridge_connected") and d.get("head", {}).get("status") == "ready")
        if d["active"] or d["session_closing"] or not d["paired_pcm"] or not d["volume"]:
            raise RuntimeError("Apparaat of bijgewerkte bridge niet gereed voor de spraaktest")
        dev.command(b"TEST.RESET")
        for i in range(0, len(pcm), 640):
            dev.command(b"TEST.DATA " + base64.b64encode(pcm[i:i+640]))
        dev.command(b"TEST.START")
        try:
            first = wait(dev, 18, lambda d: d["active"])
            began = time.monotonic()
            print("OpenAI-sessie actief. Begin akoestische test.", flush=True)
            for turn in range(args.turns):
                due = began + turn * args.seconds / args.turns
                wait(dev, args.seconds + 5, lambda d: time.monotonic() >= due and not d["speaking"], in_session=True)
                # The model may start its greeting between DIAG and TEST.SAY.
                # Retry only an explicitly refused command, never a lost ACK.
                def prompt_accepted(d):
                    return not d["speaking"] and dev.send(b"TEST.SAY", b"TEST ") == b"OK"
                before = wait(dev, 30, prompt_accepted, in_session=True)
                # Count both received output and acoustic energy on the physical
                # microphone while the actual assistant PCM is playing.
                listening_peak = 0
                def answered(d):
                    nonlocal listening_peak
                    listening_peak = max(listening_peak, d.get("listening_wave_height_px", 0))
                    t, prev = d["voice_test"], before["voice_test"]
                    return (not t["prompt_playing"] and t["prompt_mic_frames"]-prev["prompt_mic_frames"] >= 6
                            and t["voiced_reply_frames"]-prev["voiced_reply_frames"] >= 35
                            and t["reply_mic_frames"]-prev["reply_mic_frames"] >= 12)
                reply = wait(dev, 28, answered, in_session=True)
                if args.stall_ms and turn == min(1, args.turns-1):
                    dev.command(b"TEST.JITTER1550" if args.stall_ms == 1550 else b"TEST.JITTER")
                    print(f"Tijdens dit antwoord pauzeert het apparaat het netwerk {args.stall_ms} ms.", flush=True)
                if args.tx_busy and turn == 0:
                    dev.command(b"TEST.TXBUSY")
                    print("De microfoonverbinding meldt 3,2 seconden een volle zendbuffer.", flush=True)
                last = reply["voice_test"]["voiced_reply_frames"]
                quiet_since = time.monotonic()
                speech_tail_seen = False
                def finished(d):
                    nonlocal last, quiet_since, speech_tail_seen
                    voiced = d["voice_test"]["voiced_reply_frames"]
                    if d["speaking"] or voiced != last:
                        quiet_since = time.monotonic()
                    speech_tail_seen |= bool(d.get("display_speaking") and not d["speaking"])
                    last = voiced
                    return time.monotonic()-quiet_since >= 1.6
                after = wait(dev, 35, finished, in_session=True)
                turns.append({"turn": turn+1, "reply_frames": after["net_output_frames"]-before["net_output_frames"],
                              "voiced_reply_frames": after["voice_test"]["voiced_reply_frames"]-before["voice_test"]["voiced_reply_frames"],
                              "prompt_mic_frames": after["voice_test"]["prompt_mic_frames"]-before["voice_test"]["prompt_mic_frames"],
                              "reply_mic_frames": after["voice_test"]["reply_mic_frames"]-before["voice_test"]["reply_mic_frames"],
                              "gestures": after["head"]["count"]-before["head"]["count"]})
                if args.wave:
                    for name in ("listening_wave_frames", "speaking_wave_frames"):
                        turns[-1][name] = after[name] - before[name]
                    if "listening_wave_height_px" in after:
                        turns[-1]["listening_peak_half_height_px"] = listening_peak
                    if "display_speaking" in after:
                        turns[-1]["speech_tail_seen"] = speech_tail_seen
                        turns[-1]["returned_to_listening"] = not after["display_speaking"]
                print(turns[-1], flush=True)
            final = wait(dev, args.seconds+3, lambda d: time.monotonic()-began >= args.seconds, in_session=True)
            duration = round(time.monotonic()-began, 1)
        finally:
            if dev.diag()["voice_test"]["active"]:
                dev.command(b"TEST.STOP")
        closed = wait(dev, 12, lambda d: not d["active"] and not d["session_closing"])
        if args.wave:
            wait(dev, 3, lambda d: d["state"] == "Gesprek gestopt" and d["controls_visible"])
    with BRIDGE_LOG.open("rb") as log:
        log.seek(offset)
        events = [json.loads(line.split(b" ", 1)[1]) for line in log if b' {"' in line]
    starts = [e for e in events if e["event"] == "session_start"]
    ends = [e for e in events if e["event"] == "session_closed"]
    if len(starts) != 1 or len(ends) != 1 or starts[0]["session"] != ends[0]["session"]:
        raise RuntimeError("Geen eenduidige bridge-afsluiting voor deze test")
    end = ends[0]
    counters = {k: final[k]-first[k] for k in ("read_timeouts", "audio_write_errors", "capture_overflows", "playback_overflows", "voiced_gap_ms")}
    report = {"duration_s": duration, "turns": turns, "device": counters,
              "max_send_ms": final["send_max_ms"], "wifi_rssi": final["wifi_rssi"], "stop": closed["last_stop"],
              "injected_stall_ms": args.stall_ms, "injected_tx_busy_ms": 3200 if args.tx_busy else 0,
              "tx_wait_max_ms": final.get("tx_wait_max_ms", 0), "prebuffer_frames": final["prebuffer_frames"],
              "head_faults": final["head"]["faults"], "bridge_reason": end["reason"],
              "usage_confirmed": end["usage_confirmed"], "bridge_input_overflows": end["input_overflows"],
              "bridge_output_overflows": end["output_overflows"]}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    if (any(counters.values()) or (args.tx_busy and report["tx_wait_max_ms"] < 3000)
            or final["head"]["faults"] or sum(t["gestures"] for t in turns) < 1
            or end["input_overflows"] or end["output_overflows"] or not end["usage_confirmed"]
            or closed["last_stop"] != "user_stop" or end["reason"] != "user_stop"):
        raise RuntimeError("Een of meer controles op audio, beweging of afsluiting zijn mislukt")
    if args.wave and any(min(turn["listening_wave_frames"], turn["speaking_wave_frames"]) < 10 for turn in turns):
        raise RuntimeError("De luistergolf of spraakgolf is onvoldoende bijgewerkt")
    if args.wave and "display_speaking" in final:
        if not any(turn["speech_tail_seen"] for turn in turns) or not all(turn["returned_to_listening"] for turn in turns):
            raise RuntimeError("Het vasthouden of loslaten van de spreekweergave is niet gemeten")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", required=True)
    ap.add_argument("--wav", type=Path, help="Korte synthetische Nederlandse vraag, mono PCM16 op 16 kHz")
    ap.add_argument("--seconds", type=int, default=110)
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--report", type=Path)
    stall = ap.add_mutually_exclusive_group()
    stall.add_argument("--stall", dest="stall_ms", action="store_const", const=650, default=0,
                       help="Voeg eenmaal een netwerkpauze van 650 ms toe tijdens een antwoord")
    stall.add_argument("--stall-ms", type=int, choices=(650, 1550), default=0)
    ap.add_argument("--tx-busy", action="store_true", help="Simuleer een volle TCP-zendbuffer gedurende 3,2 s")
    ap.add_argument("--wave", action="store_true", help="Controleer de luistergolf en spraakgolf tijdens elke beurt")
    args = ap.parse_args()
    if not 15 <= args.seconds <= 130 or not 1 <= args.turns <= 4:
        ap.error("Kies 15–130 seconden en 1–4 vragen")
    with tempfile.TemporaryDirectory() as tmp:
        wav = args.wav or Path(tmp) / "prompt.wav"
        if not args.wav:
            subprocess.run(["say", "-v", "Xander", "-r", "170", "-o", str(wav), "--file-format=WAVE",
                            "--data-format=LEI16@16000", QUESTION], check=True)
        run(args, prompt(wav))
