"""ffmpeg: не больше media.FFMPEG_PARALLEL процессов сразу, сколько бы записей ни шло."""
import subprocess
import threading
import time
import types

from app import media, speech


def test_no_more_than_two_ffmpeg_at_once(monkeypatch, tmp_path):
    lock, state = threading.Lock(), types.SimpleNamespace(now=0, most=0, calls=[])

    def fake_run(args, **kwargs):
        with lock:
            state.now += 1
            state.most = max(state.most, state.now)
            state.calls.append(args[0])
        time.sleep(0.05)
        with lock:
            state.now -= 1
        # пик не тишина: ensure_audio после извлечения меряет уровень звука
        return subprocess.CompletedProcess(args, 0, stdout="0\n", stderr="max_volume: -12.0 dB")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    monkeypatch.setattr(media, "has_audio_stream", lambda path: True)
    monkeypatch.setattr(media.Path, "replace", lambda self, target: None)
    src = tmp_path / "запись.mp4"
    # пять записей сразу: и извлечение звука, и нарезка частей для распознавания
    jobs = [threading.Thread(target=media.ensure_audio, args=(src, tmp_path / str(n), "запись", True, lambda s: None))
            for n in range(5)]
    jobs += [threading.Thread(target=speech.make_chunk, args=(src, tmp_path / f"{n}.mp3", 0.0, 10.0))
             for n in range(5)]
    for t in jobs:
        t.start()
    for t in jobs:
        t.join(10)
    assert media.FFMPEG_PARALLEL == 2 and state.most == 2
    assert len(state.calls) == 15 and set(state.calls) == {"ffmpeg"}       # 5 × (извлечь + уровень) + 5 нарезок
