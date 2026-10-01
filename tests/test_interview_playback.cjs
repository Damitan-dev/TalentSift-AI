// Run the actual candidate page code with controlled audio clocks and sources.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../client/index.html'), 'utf8');

function setup() {
    const elements = new Map(), intervals = new Map(), timeouts = new Map();
    let timerId = 0;
    function element() {
        return {textContent:'',value:'',hidden:true,disabled:false,dataset:{},children:[],
            appendChild(child) { child.parent=this; this.children.push(child); },
            remove() { if(this.parent) this.parent.children=this.parent.children.filter(x=>x!==this); }
        };
    }
    class AudioClock {
        constructor() { this.state='running'; this.currentTime=0; this.sources=[]; }
        resume() { this.state='running'; return Promise.resolve(); }
        createGain() { return {connect(){},gain:{value:1,cancelScheduledValues(){},
            setValueAtTime(){},linearRampToValueAtTime(){}}}; }
        createBuffer(channels,length,rate) { return {duration:length/rate,copyToChannel(){}}; }
        createBufferSource() {
            const source = {connect(){},start(at){this.startAt=at;},stop(){},
                end(){this.onended?.();}};
            this.sources.push(source);
            return source;
        }
    }
    class Socket {
        static OPEN=1;
        constructor() { this.readyState=1; this.sent=[]; }
        send(raw) { this.sent.push(JSON.parse(raw)); }
        close() { this.readyState=3; }
    }
    const scope = {console:{log(){},warn(){},error(){}},URLSearchParams,
        window:{location:{hash:'',pathname:'/',protocol:'https:',host:'example.test',search:''},
            history:{replaceState(){}},addEventListener(){}},
        document:{getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},
            createElement:element,querySelectorAll(){return[];}},
        fetch:async()=>({ok:false,json:async()=>({detail:'No invitation'})}),
        WebSocket:Socket,AudioContext:AudioClock,
        atob:raw=>Buffer.from(raw,'base64').toString('binary'),
        setInterval(callback){const id=++timerId;intervals.set(id,callback);return id;},
        clearInterval(id){intervals.delete(id);},
        setTimeout(callback){const id=++timerId;timeouts.set(id,callback);return id;},
        clearTimeout(id){timeouts.delete(id);}
    };
    const context=vm.createContext(scope);
    vm.runInContext([...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n'),context);
    vm.runInContext('sessionId="test-session"; playCtx=new AudioContext(); connectInterviewWebSocket();',context);
    const evaluate=code=>vm.runInContext(code,context);
    const clock=evaluate('playCtx'), socket=evaluate('ws');
    return {clock,socket,elements,evaluate,
        message(msg){socket.onmessage({data:JSON.stringify(msg)});},
        tick(){for(const callback of [...intervals.values()])callback();},
        rows(){return elements.get('transcript')?.children || [];},
        flushTimeouts(){for(const callback of [...timeouts.values()])callback();timeouts.clear();}
    };
}

function audio(state,item='opening-audio',response='opening',seconds=0.2) {
    state.message({type:'audio',item_id:item,response_id:response,content_index:0,
        data:Buffer.alloc(Math.round(seconds*24000)*2).toString('base64')});
    return state.clock.sources.at(-1);
}

test('opening waits for real source completion and acknowledges the correct response once',()=>{
    const s=setup(), source=audio(s);
    s.message({type:'opening_generated',response_id:'opening',item_ids:['opening-audio']});
    assert.equal(s.evaluate('candidateAudioEnabled'),false);
    s.clock.currentTime=5;
    s.tick(); s.flushTimeouts();
    assert.equal(s.socket.sent.length,0,'Elapsed wall time cannot substitute for source completion');
    source.end(); s.tick();
    assert.deepEqual(s.socket.sent,[{type:'opening_playback_finished',response_id:'opening'}]);
    assert.equal(s.evaluate('candidateAudioEnabled'),true);
    s.tick();
    assert.equal(s.socket.sent.length,1);
    assert.ok(source.startAt>0,'Startup buffer protects the first audio chunk');
});

test('a suspended audio clock cannot finish the opening',()=>{
    const s=setup(), source=audio(s);
    s.message({type:'opening_generated',response_id:'opening',item_ids:['opening-audio']});
    s.clock.state='suspended';
    s.clock.currentTime=1;
    source.end(); s.tick(); s.flushTimeouts();
    assert.equal(s.socket.sent.length,0);
    s.clock.state='running';s.tick();
    assert.equal(s.socket.sent[0].type,'opening_playback_finished');
});

test('all opening chunks finish before listening, including multiple audio items',()=>{
    const s=setup(), first=audio(s,'one'), second=audio(s,'two');
    s.message({type:'opening_generated',response_id:'opening',item_ids:['one','two']});
    s.clock.currentTime=1;first.end();s.tick();
    assert.equal(s.socket.sent.length,0);
    second.end();s.tick();
    assert.equal(s.socket.sent.length,1);
    assert.ok(second.startAt>=first.startAt+first.buffer.duration);
});

test('Bianca deltas appear during playback and final text waits for the entire response',()=>{
    const s=setup(), first=audio(s,'question','reply',0.4);
    s.message({type:'transcript_delta',speaker:'interviewer',item_id:'question',response_id:'reply',text:'Tell me'});
    assert.equal(s.rows().length,0,'Captions must not start before audible playback');
    s.clock.currentTime=first.startAt;s.tick();
    assert.equal(s.rows()[0].textContent,'interviewer: Tell me');
    const second=audio(s,'question','reply',0.4);
    s.message({type:'transcript_delta',speaker:'interviewer',item_id:'question',response_id:'reply',text:' about Python.'});
    s.message({type:'transcript',speaker:'interviewer',item_id:'question',response_id:'reply',text:'Tell me about Python.'});
    s.message({type:'response_finished',response_id:'reply',status:'completed',item_ids:['question']});
    assert.equal(s.rows()[0].textContent,'interviewer: Tell me');
    s.clock.currentTime=second.startAt;first.end();s.tick();
    assert.equal(s.rows()[0].textContent,'interviewer: Tell me about Python.');
    assert.equal(s.rows()[0].className,'transcript-live interviewer');
    s.clock.currentTime=second.startAt+second.buffer.duration;second.end();s.tick();
    assert.equal(s.rows()[0].className,'transcript-turn interviewer');
    assert.equal(s.rows().length,1,'Final text updates the same paragraph');
});

test('late text from an interrupted response cannot affect a newer reply',()=>{
    const s=setup(), first=audio(s,'old','old-response');
    s.message({type:'transcript_delta',speaker:'interviewer',item_id:'old',response_id:'old-response',text:'Old words'});
    s.clock.currentTime=first.startAt;s.tick();
    s.message({type:'interrupt',response_id:'old-response'});
    const next=audio(s,'new','new-response');
    s.message({type:'transcript',speaker:'interviewer',item_id:'old',response_id:'old-response',text:'Unheard old tail'});
    s.message({type:'transcript_delta',speaker:'interviewer',item_id:'new',response_id:'new-response',text:'New question'});
    s.clock.currentTime=next.startAt;s.tick();s.flushTimeouts();
    assert.equal(s.rows().length,2);
    assert.equal(s.rows()[0].textContent,'interviewer: Old words');
    assert.equal(s.rows()[1].textContent,'interviewer: New question');
});

test('early candidate captions link to their final turn without duplicating text',()=>{
    const s=setup();
    s.message({type:'transcript_delta',speaker:'candidate',item_id:'live:one',text:'I am David'});
    assert.equal(s.rows()[0].textContent,'candidate: I am David');
    s.message({type:'transcript_link',provisional_item_id:'live:one',item_id:'main-one'});
    s.message({type:'transcript',speaker:'candidate',item_id:'main-one',text:'I am David Afolabi.'});
    s.message({type:'transcript_delta',speaker:'candidate',item_id:'live:one',text:' incorrect late words'});
    assert.equal(s.rows().length,1);
    assert.equal(s.rows()[0].textContent,'candidate: I am David Afolabi.');
    assert.equal(s.rows()[0].dataset.itemId,'main-one');
});

test('a final transcript arriving before its preview link wins, including empty speech',()=>{
    const s=setup();
    s.message({type:'transcript_delta',speaker:'candidate',item_id:'live:one',text:'Wrong preview'});
    s.message({type:'transcript',speaker:'candidate',item_id:'main-one',text:'Correct answer'});
    s.message({type:'transcript_link',provisional_item_id:'live:one',item_id:'main-one'});
    assert.equal(s.rows().length,1);
    assert.equal(s.rows()[0].textContent,'candidate: Correct answer');
    s.message({type:'transcript_delta',speaker:'candidate',item_id:'live:two',text:'Noise hallucination'});
    s.message({type:'transcript_retract',item_id:'main-two'});
    s.message({type:'transcript_link',provisional_item_id:'live:two',item_id:'main-two'});
    assert.equal(s.rows().length,1);
});

test('out-of-order final candidate text never erases a newer live answer',()=>{
    const s=setup();
    s.message({type:'transcript_delta',speaker:'candidate',item_id:'one',text:'First answer'});
    s.message({type:'transcript_delta',speaker:'candidate',item_id:'two',text:'Second'});
    s.message({type:'transcript',speaker:'candidate',item_id:'one',text:'First corrected answer.'});
    s.message({type:'transcript_delta',speaker:'candidate',item_id:'two',text:' answer'});
    assert.equal(s.rows()[0].textContent,'candidate: First corrected answer.');
    assert.equal(s.rows()[1].textContent,'candidate: Second answer');
});

test('closing completion also follows the audio clock and actual source completion',()=>{
    const s=setup(), source=audio(s,'bye','closing');
    s.message({type:'closing_generated',response_id:'closing',item_ids:['bye']});
    s.clock.currentTime=1;s.tick();
    assert.equal(s.socket.sent.length,0);
    source.end();s.tick();
    assert.deepEqual(s.socket.sent,[{type:'closing_playback_finished',response_id:'closing'}]);
});
