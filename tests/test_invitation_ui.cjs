const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../client/index.html'), 'utf8');

async function setup(hash, valid=true) {
    const elements = new Map(), calls = [], history = [];
    const scope = {
        console: {log(){},warn(){},error(){}}, URLSearchParams,
        window: {location: {hash,pathname:'/',protocol:'http:',host:'localhost',search:''},
                 history:{replaceState(...args){history.push(args)}},addEventListener(){}},
        document: {getElementById(id){
            if(!elements.has(id)) elements.set(id, {value:'',hidden:true,disabled:true,textContent:'',checked:false});
            return elements.get(id);
        },querySelectorAll(){return[];}},
        async fetch(url, options={}) {
            calls.push({url, options});
            if(url==='/api/session') return {ok:true,async json(){return {session_id:'session-one',recording_token:null}}};
            return {ok:valid,async json(){return valid
                ? {job_id:'job-one',title:'Python Developer',candidate_name:'David Adebayo',csrf_token:'test-csrf'}
                : {detail:'This invitation has expired.'}}};
        }, setTimeout,clearTimeout,setInterval,clearInterval
    };
    const context = vm.createContext(scope);
    vm.runInContext([...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n'),context);
    await new Promise(setImmediate);
    return {elements,calls,history,context};
}

test('personal invitation displays server name and removes the secret from address bar',async()=>{
    const {elements,calls,history}=await setup('#invite=secret-from-recruiter');
    assert.equal(calls[0].url,'/api/invitations/exchange');
    assert.deepEqual(JSON.parse(calls[0].options.body),{token:'secret-from-recruiter'});
    assert.equal(elements.get('candidate-name').value,'David Adebayo');
    assert.equal(elements.get('candidate-welcome').textContent,'Welcome, David Adebayo');
    assert.equal(elements.get('welcome-start').disabled,false);
    assert.equal(history[0][2],'/');
    assert.match(html, /id="candidate-name"[^>]*readonly/);
});

test('candidate preparation sends consent and csrf, never an editable name or job ID',async()=>{
    const {elements,calls,context}=await setup('#invite=secret');
    for (const id of ['consent-checkbox','language','record-audio-checkbox']) context.document.getElementById(id);
    elements.get('consent-checkbox').checked=true;
    elements.get('language').value='en';
    elements.get('candidate-name').value='Maliciously changed name';
    await elements.get('consent-continue').onclick();
    const request=calls.find(c=>c.url==='/api/session');
    assert.deepEqual(JSON.parse(request.options.body),{language:'en',consent:true,record_audio:false});
    assert.equal(request.options.headers['X-CSRF-Token'],'test-csrf');
});

test('refresh restores unused invitation with the server cookie',async()=>{
    const {calls,elements,history}=await setup('');
    assert.equal(calls[0].url,'/api/invitation');
    assert.equal(elements.get('welcome-start').disabled,false);
    assert.equal(history.length,0);
});

test('expired invitation keeps interview start disabled and explains why',async()=>{
    const {calls,elements,history}=await setup('#invite=expired',false);
    assert.equal(elements.get('welcome-start').disabled,true);
    assert.equal(elements.get('invitation-error').textContent,'This invitation has expired.');
    assert.equal(elements.get('invitation-error').hidden,false);
    assert.equal(history.length,0);
    assert.equal(calls.length,1);
});
