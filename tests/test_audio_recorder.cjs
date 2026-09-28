// Browser lifecycle contract: final blob first, save acknowledgement last.
// Run with: node --test tests/test_audio_recorder.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup({supported = true} = {}) {
    class Socket {
        static OPEN = 1;
        constructor() { this.readyState = 1; this.bufferedAmount = 0; this.sent = []; }
        send(value) { this.sent.push(value); }
        close() { this.readyState = 3; this.onclose?.(); }
        message(value) { this.onmessage({data: JSON.stringify(value)}); }
    }
    class Encoder {
        static isTypeSupported() { return supported; }
        constructor() { this.state = 'inactive'; this.mimeType = 'audio/webm'; }
        start() { this.state = 'recording'; }
        stop() {
            this.state = 'inactive';
            queueMicrotask(() => {
                this.ondataavailable({data: new Blob(['final audio'])});
                this.onstop();
            });
        }
    }
    const node = () => ({gain: {}, connect() {}, disconnect() {}});
    const status = [];
    const scope = {
        window: {}, location: {protocol: 'https:', host: 'talentsift.example'},
        MediaRecorder: Encoder, WebSocket: Socket, Blob, setTimeout, clearTimeout
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../client/audio-recorder.js'), 'utf8'), scope);
    const recorder = new scope.window.InterviewAudioRecorder({
        context: {
            createGain: node,
            createMediaStreamDestination: () => ({stream: {getTracks: () => [{stop() {}}]}})
        }, microphone: node(), onStatus: value => status.push(value)
    });
    return {recorder, status};
}

async function connected() {
    const state = setup();
    const ready = state.recorder.connect('session-id', 'private-token');
    state.recorder.socket.onopen();
    state.recorder.socket.message({type: 'recording_ready'});
    assert.equal(await ready, true);
    state.recorder.start();
    return state;
}

test('sends final audio before finish and waits for server acknowledgement', async () => {
    const {recorder, status} = await connected();
    const done = recorder.finish(true);
    await new Promise(resolve => setImmediate(resolve));
    const sent = recorder.socket.sent;
    assert.ok(sent.at(-2) instanceof Blob);
    assert.equal(await sent.at(-2).text(), 'final audio');
    assert.deepEqual(JSON.parse(sent.at(-1)), {type: 'finish', complete: true});
    assert.equal(recorder.phase, 'finishing');
    recorder.socket.message({type: 'recording_saved', status: 'saved', bytes: 11});
    assert.equal(await done, true);
    assert.equal(await recorder.finish(false), true);
    assert.match(status.at(-1), /recording saved/);
});

test('recording transport failure ends recording without touching interview transport', async () => {
    const {recorder, status} = await connected();
    recorder.socket.close();
    assert.equal(await recorder.done, false);
    assert.equal(recorder.phase, 'failed');
    assert.match(status.at(-1), /interview can continue/);
});

test('unsupported encoder reports unavailability and resolves startup', async () => {
    const {recorder, status} = setup({supported: false});
    assert.equal(await recorder.connect('session-id', 'private-token'), false);
    assert.match(status.at(-1), /unavailable in this browser/);
});

test('oversized pending upload fails without unbounded buffering', async () => {
    const {recorder} = await connected();
    recorder.socket.bufferedAmount = 4 * 1024 * 1024;
    recorder.encoder.ondataavailable({data: new Blob(['more audio'])});
    assert.equal(await recorder.done, false);
    assert.equal(recorder.phase, 'failed');
});
