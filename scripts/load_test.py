"""
load_test.py - Concurrency and latency benchmark for TaleemGo WhatsApp bot.

Simulates N concurrent users sending a realistic mix of messages:
1. attendance (command)
2. assignments (command)
3. natural-language sentence (requiring LLM reasoning)
4. plan ("plan AI 80")
5. voice payload (simulated audio message)

Against real handle() / /incoming logic with:
- Fake LMS that sleeps 300-800ms per call
- Fake LLM provider that sleeps 2-5s per call
- Fake STT that sleeps 1-2s
- Fake bridge send (10-30ms)

Outputs per-stage timings (queue wait, LMS, LLM, STT, send) and p50/p95 end-to-end per message type.
"""

import argparse
import base64
import collections
import concurrent.futures
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure root directory and app directory are on path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "app"))

import app.wa_bot as wa_bot
usr = wa_bot.usr

# Configure thread safety and metrics collection
metrics_lock = threading.Lock()
stage_timings = collections.defaultdict(list)  # stage -> list of durations (sec)
e2e_timings = collections.defaultdict(list)    # msg_type -> list of durations (sec)


def record_stage(stage: str, duration: float):
    with metrics_lock:
        stage_timings[stage].append(duration)


def percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    sorted_d = sorted(data)
    k = (len(sorted_d) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_d) - 1)
    d = k - f
    return sorted_d[f] + d * (sorted_d[c] - sorted_d[f])


class FakeLMSClient:
    def __init__(self, user_id="student_1", min_sleep=0.3, max_sleep=0.8):
        self.user_id = user_id
        self.min_sleep = min_sleep
        self.max_sleep = max_sleep
        self.student_id = "fake_student_123"

    def _sleep(self):
        dur = random.uniform(self.min_sleep, self.max_sleep)
        t0 = time.time()
        time.sleep(dur)
        record_stage("lms", time.time() - t0)

    def get_courses(self, *args, **kwargs):
        self._sleep()
        return [
            {"id": "c1", "name": "Artificial Intelligence", "code": "CS-401", "semester": "Fall", "year": "2026"},
            {"id": "c2", "name": "Human Computer Interaction", "code": "CS-402", "semester": "Fall", "year": "2026"},
            {"id": "c3", "name": "Natural Language Processing", "code": "CS-403", "semester": "Fall", "year": "2026"},
        ]

    def get_attendance(self, *args, **kwargs):
        self._sleep()
        return {
            "overall": {"present": 35, "absent": 5, "other": 0, "total": 40, "percent": 87.5},
            "courses": {
                "c1": {"present": 16, "absent": 6, "other": 0, "total": 22, "percent": 72.7},
                "c2": {"present": 10, "absent": 0, "other": 0, "total": 10, "percent": 100.0},
                "c3": {"present": 9, "absent": 1, "other": 0, "total": 10, "percent": 90.0},
            },
        }

    def get_assignments(self, course_id=None, *args, **kwargs):
        self._sleep()
        return [
            {"id": "a1", "course_id": "c1", "title": "AI Assignment 1", "due_date": "2026-11-20T23:59:00Z"},
            {"id": "a2", "course_id": "c1", "title": "AI Assignment 2", "due_date": "2026-11-25T23:59:00Z"},
            {"id": "a3", "course_id": "c2", "title": "HCI Prototype", "due_date": "2026-11-18T23:59:00Z"},
            {"id": "a4", "course_id": "c3", "title": "NLP Parser", "due_date": "2026-11-10T23:59:00Z"},
        ]

    def get_submissions(self, *args, **kwargs):
        self._sleep()
        return {
            "a4": {"assignment_id": "a4", "file_name": "parser.py", "submitted_at": "2026-11-09T10:00:00Z", "grade": 95},
        }

    def get_assignments_with_status(self, course_id=None, *args, **kwargs):
        assignments = self.get_assignments(course_id)
        submissions = self.get_submissions()
        out = []
        for a in assignments:
            sub = submissions.get(a["id"])
            if sub and sub.get("file_name"):
                st = "submitted"
            elif "2026-11-10" in a["due_date"]:
                st = "overdue"
            else:
                st = "pending"
            out.append({**a, "status": st, "submission": sub})
        return out

    def get_grades(self, course_id=None):
        self._sleep()
        return []

    def get_resources(self, course_id=None):
        self._sleep()
        return [
            {"id": "r1", "course_id": "c1", "name": "Lecture 1 Intro.pdf", "file_path": "lec1.pdf"},
            {"id": "r2", "course_id": "c1", "name": "Lecture 2 Search.pdf", "file_path": "lec2.pdf"},
        ]


def fake_call_chat_completions(*args, **kwargs):
    dur = random.uniform(2.0, 5.0)
    t0 = time.time()
    time.sleep(dur)
    record_stage("llm", time.time() - t0)
    return json.dumps({
        "actions": [{"intent": "attendance"}],
        "off_topic": False,
        "unsupported": False,
        "clarify": None,
        "lang": "en"
    })


def fake_stt_transcribe(data_b64, mime="audio/ogg", seconds=3.0, lang_hint="en"):
    dur = random.uniform(1.0, 2.0)
    t0 = time.time()
    time.sleep(dur)
    record_stage("stt", time.time() - t0)
    return "attendance"


def fake_reply(chat_id: str, text: str):
    t0 = time.time()
    time.sleep(random.uniform(0.01, 0.03))  # simulate HTTP bridge call
    record_stage("send", time.time() - t0)


def setup_users(n_users: int):
    usr.init_db()
    for i in range(1, n_users + 1):
        chat_id = f"user_{i}@c.us"
        usr.upsert_user(
            chat_id,
            email=f"student{i}@univ.edu",
            status="active",
            attempt=0,
            is_admin=0,
            lang="en"
        )
        fake_client = FakeLMSClient(user_id=f"student_{i}")
        usr.set_client(chat_id, fake_client)


def run_single_request(user_idx: int, msg_type: str, text_or_payload):
    chat_id = f"user_{user_idx}@c.us"
    req_t0 = time.time()
    
    # Record dispatch/queue time if applicable
    record_stage("queue_wait", 0.0)

    try:
        if msg_type == "voice":
            wa_bot.handle_audio(chat_id, text_or_payload)
        else:
            wa_bot.handle(chat_id, text_or_payload)
    finally:
        total_time = time.time() - req_t0
        with metrics_lock:
            e2e_timings[msg_type].append(total_time)


def run_load_test(num_users: int = 5):
    print(f"\n=======================================================")
    print(f"  TaleemGo AI Concurrency Benchmark (N={num_users} users)")
    print(f"=======================================================\n")

    setup_users(num_users)

    # Realistic message mix
    mix = [
        ("attendance", "attendance"),
        ("assignments", "assignments"),
        ("natural_lang", "can you check if I am at risk of failing any course this semester"),
        ("plan", "plan AI 80"),
        ("voice", {"mime": "audio/ogg", "data_b64": base64.b64encode(b"FAKE_AUDIO_DATA").decode(), "seconds": 3.0}),
    ]

    tasks = []
    for i in range(1, num_users + 1):
        msg_type, payload = mix[(i - 1) % len(mix)]
        tasks.append((i, msg_type, payload))

    # Apply patches for LMS, LLM, STT, and reply
    with patch.object(wa_bot, "_reply", side_effect=fake_reply), \
         patch("app.llm.providers.call_chat_completions", side_effect=fake_call_chat_completions), \
         patch("app.wa_bot.call_chat_completions", side_effect=fake_call_chat_completions, create=True), \
         patch("app.llm.router.call_chat_completions", side_effect=fake_call_chat_completions), \
         patch("app.wa_bot.stt_transcribe", side_effect=fake_stt_transcribe):

        test_start = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_users) as executor:
            futures = [
                executor.submit(run_single_request, uid, mtype, payload)
                for uid, mtype, payload in tasks
            ]
            concurrent.futures.wait(futures)
        total_test_duration = time.time() - test_start

    # Report results
    print(f"Completed {len(tasks)} requests across {num_users} simultaneous users in {total_test_duration:.2f}s.\n")

    print("--- Per-Stage Latency Breakdown (Mean, p50, p95) ---")
    stages = ["queue_wait", "lms", "llm", "stt", "send"]
    for s in stages:
        data = stage_timings[s]
        if data:
            mean_v = sum(data) / len(data)
            p50_v = percentile(data, 50)
            p95_v = percentile(data, 95)
            calls = len(data)
            print(f"  {s:<12}: calls={calls:<3} mean={mean_v*1000:6.1f}ms  p50={p50_v*1000:6.1f}ms  p95={p95_v*1000:6.1f}ms")
        else:
            print(f"  {s:<12}: calls=0   (not invoked)")

    print("\n--- End-to-End Latency by Message Type (p50, p95) ---")
    all_e2e = []
    for mtype, _ in mix:
        data = e2e_timings[mtype]
        if data:
            all_e2e.extend(data)
            p50_v = percentile(data, 50)
            p95_v = percentile(data, 95)
            print(f"  {mtype:<14}: count={len(data):<2} p50={p50_v:5.2f}s  p95={p95_v:5.2f}s")

    if all_e2e:
        print(f"\n  Overall End-to-End: p50={percentile(all_e2e, 50):5.2f}s  p95={percentile(all_e2e, 95):5.2f}s")
    print(f"=======================================================\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TaleemGo Load Test")
    parser.add_argument("--users", type=int, default=5, help="Number of concurrent users (default: 5)")
    args = parser.parse_args()
    run_load_test(args.users)
