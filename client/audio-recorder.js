/* The microphone and Bianca's playback feed a separate, silent recording mix.
 * Encoder/upload failures never close or change the interview WebSocket. */
class InterviewAudioRecorder {
    constructor({context, microphone, onStatus}) {
        this.context = context;
        this.microphone = microphone;
        this.onStatus = onStatus;
        this.phase = "idle";
        this.interviewer = null;
        this.complete = false;
        this.done = new Promise(resolve => { this.resolveDone = resolve; });
    }

    async connect(sessionId, token) {
        const mimeType = typeof MediaRecorder !== "undefined" && [
            "audio/webm;codecs=opus", "audio/webm",
            "audio/ogg;codecs=opus", "audio/mp4"
        ].find(type => MediaRecorder.isTypeSupported(type));
        if (!mimeType) {
            this.fail("Audio recording is unavailable in this browser. Your interview can continue.");
            return false;
        }
        this.phase = "connecting";
        this.onStatus("Preparing audio recordingâ€¦");
        const ready = new Promise(resolve => { this.resolveReady = resolve; });
        try {
            this.destination = this.context.createMediaStreamDestination();
            this.destination.channelCount = 1;
            this.mix = this.context.createGain();
            this.mix.gain.value = 0.5; // Leave headroom when both people speak.
            this.microphone.connect(this.mix);
            this.mix.connect(this.destination);
            this.encoder = new MediaRecorder(this.destination.stream, {
                mimeType, audioBitsPerSecond: 96000
            });
            const proto = location.protocol === "https:" ? "wss" : "ws";
            this.socket = new WebSocket(`${proto}://${location.host}/ws/recording/${encodeURIComponent(sessionId)}`);
            this.readyTimer = setTimeout(() => this.fail(), 8000);
            this.socket.onopen = () => this.socket.send(JSON.stringify({
                type: "start", token, mime_type: this.encoder.mimeType || mimeType
            }));
            this.socket.onmessage = event => {
                let message;
                try { message = JSON.parse(event.data); } catch (_) { this.fail(); return; }
                if (message.type === "recording_ready" && this.phase === "connecting") {
                    clearTimeout(this.readyTimer);
                    this.phase = "ready";
                    this.onStatus("Audio recording will begin with the interview.");
                    this.resolveReady(true);
                } else if (message.type === "recording_saved" && this.phase === "finishing") {
                    clearTimeout(this.finishTimer);
                    const saved = message.status === "saved" && message.bytes > 0;
                    this.phase = "saved";
                    this.onStatus(saved ? "Audio recording saved. You can close this page."
                        : "Recording ended. Any received audio was kept, but the recording may be incomplete.");
                    this.cleanup();
                    this.resolveDone(saved);
                    this.socket.close();
                } else if (message.type === "recording_error") {
                    this.fail();
                }
            };
            this.socket.onerror = () => this.fail();
            this.socket.onclose = () => {
                if (!["saved", "failed"].includes(this.phase)) this.fail();
            };
            this.encoder.ondataavailable = event => {
                if (!event.data.size || !["recording", "finishing"].includes(this.phase)) return;
                if (this.socket.readyState !== WebSocket.OPEN ||
                    this.socket.bufferedAmount + event.data.size > 4 * 1024 * 1024) {
                    this.fail();
                    return;
                }
                // send(Blob) preserves order, including before the finish JSON.
                // Slice delayed mobile-browser chunks to bound server messages.
                try {
                    for (let offset = 0; offset < event.data.size; offset += 256 * 1024) {
                        this.socket.send(event.data.slice(offset, offset + 256 * 1024));
                    }
                } catch (_) { this.fail(); }
            };
            this.encoder.onerror = () => this.fail();
            this.encoder.onstop = () => {
                if (this.phase === "recording") {
                    // Unexpected encoder stop still preserves the final chunk.
                    this.phase = "finishing";
                    this.complete = false;
                    this.finishTimer = setTimeout(() => this.fail(), 15000);
                }
                if (this.phase === "finishing" && this.socket.readyState === WebSocket.OPEN) {
                    try {
                        this.socket.send(JSON.stringify({type: "finish", complete: this.complete}));
                    } catch (_) { this.fail(); }
                }
            };
        } catch (_) {
            this.fail();
        }
        return ready;
    }

    addInterviewer(node) {
        if (!this.mix || this.interviewer || ["saved", "failed"].includes(this.phase)) return;
        this.interviewer = node;
        // Tap after the playback gain so fades and interruptions are recorded.
        try { node.connect(this.mix); } catch (_) { this.fail(); }
    }

    start() {
        if (this.phase !== "ready") return;
        try {
            this.encoder.start(1000);
            this.phase = "recording";
            this.onStatus("Recording interview audioâ€¦");
            this.limitTimer = setTimeout(() => this.finish(false), 14 * 60 * 1000);
        } catch (_) { this.fail(); }
    }

    finish(complete = true) {
        if (["finishing", "saved", "failed"].includes(this.phase)) return this.done;
        if (!["recording", "ready"].includes(this.phase)) {
            this.fail();
            return this.done;
        }
        this.complete = complete;
        this.phase = "finishing";
        clearTimeout(this.limitTimer);
        this.onStatus("Saving interview audioâ€¦ Please keep this page open.");
        this.finishTimer = setTimeout(() => this.fail(), 15000);
        try {
            if (this.encoder.state !== "inactive") {
                // Final dataavailable arrives before onstop sends finish.
                this.encoder.stop();
            } else {
                this.socket.send(JSON.stringify({type: "finish", complete}));
            }
        } catch (_) { this.fail(); }
        return this.done;
    }

    fail(message = "Audio recording stopped. Your interview can continue; some audio may be missing.") {
        if (["saved", "failed"].includes(this.phase)) return;
        this.phase = "failed";
        clearTimeout(this.readyTimer);
        clearTimeout(this.finishTimer);
        clearTimeout(this.limitTimer);
        if (this.encoder && this.encoder.state !== "inactive") {
            try { this.encoder.stop(); } catch (_) { /* already stopping */ }
        }
        this.socket?.close();
        this.cleanup();
        this.onStatus(message);
        this.resolveReady?.(false);
        this.resolveDone(false);
    }

    cleanup() {
        clearTimeout(this.limitTimer);
        if (this.mix) {
            try { this.microphone.disconnect(this.mix); } catch (_) { /* disconnected */ }
            try { this.interviewer?.disconnect(this.mix); } catch (_) { /* disconnected */ }
            this.mix.disconnect();
        }
        this.destination?.stream.getTracks().forEach(track => track.stop());
    }
}

window.InterviewAudioRecorder = InterviewAudioRecorder;