// Exercise the shipped client at real schedule boundaries without a browser clock dependency.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../app/static/site-appearance.js'), 'utf8');
const epoch = Date.parse('2026-11-11T00:00:00Z');

function page(overrides = {}) {
    const config = {configured_theme:'pepero', color_mode:'light', motion_enabled:true,
        server_now:new Date(epoch).toISOString(), starts_at:null, ends_at:null, ...overrides};
    const classes = new Set();
    const timers = new Map();
    const listeners = new Map();
    let tick = 0, sequence = 0, intervalCount = 0, fetchCount = 0;
    let nextReply = null;
    const codeInput = {value:'print("keep my unsent code")'};
    const document = {hidden:false, body:{classList:{
        toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
        contains(name) { return classes.has(name); },
    }, style:{setProperty(){}}}, getElementById(id) {
        return id === 'site-appearance-config' ? {textContent:JSON.stringify(config)} : codeInput;
    }, addEventListener(name, action){listeners.set(name, action);}};
    const window = {addEventListener(name, action){listeners.set(name, action);}, dispatchEvent(){}};
    vm.runInNewContext(script, {document, window, Date, JSON, Number, Event,
        performance:{now:()=>tick}, matchMedia:()=>({matches:false}),
        setTimeout(action, delay){const id=++sequence; timers.set(id, {at:tick+delay, action}); return id;},
        clearTimeout(id){timers.delete(id);}, setInterval(){intervalCount++;},
        fetch:async()=>{fetchCount++; if (!nextReply) throw Error('offline'); return {ok:true,json:async()=>nextReply};},
        requestAnimationFrame(){},
    });
    return {classes, codeInput, timers, listeners,
        get intervalCount(){return intervalCount;}, get fetchCount(){return fetchCount;},
        reply(value){nextReply=value;},
        async refresh(){await listeners.get('pageshow')?.();},
        advance(ms){
            const target = tick + ms;
            while (true) {
                const next = [...timers].sort((a,b)=>a[1].at-b[1].at).find(([,v])=>v.at<=target);
                if (!next) break;
                tick=next[1].at; timers.delete(next[0]); next[1].action();
            }
            tick=target;
        },
    };
}

test('future event starts and ends in an open problem page, retaining code', () => {
    const app=page({starts_at:new Date(epoch+1000).toISOString(), ends_at:new Date(epoch+2000).toISOString()});
    const code=app.codeInput.value;
    assert.equal(app.classes.has('event-theme'), false);
    app.advance(1010);
    assert.equal(app.classes.has('event-light'), true);
    assert.equal(app.codeInput.value, code);
    app.advance(1000);
    assert.equal(app.classes.has('event-theme'), false);
    assert.equal(app.codeInput.value, code);
    assert.equal(app.fetchCount, 0); // Known deadlines work even while offline.
});

test('immediate unlimited dark theme never schedules an end', () => {
    const app=page({color_mode:'dark', motion_enabled:false});
    assert.equal(app.classes.has('event-dark'), true);
    assert.equal(app.classes.has('event-still'), true);
    assert.equal(app.timers.size, 0);
    app.advance(10*365*86400000);
    assert.equal(app.classes.has('event-theme'), true);
});

test('new server settings replace an old timer without navigation', async () => {
    const app=page({ends_at:new Date(epoch+1000).toISOString()});
    app.reply({configured_theme:'pepero',color_mode:'dark',motion_enabled:true,server_now:new Date(epoch).toISOString(),starts_at:null,ends_at:null});
    await app.refresh();
    assert.equal(app.classes.has('event-dark'), true);
    assert.equal(app.timers.size, 0);
    app.advance(2000);
    assert.equal(app.classes.has('event-theme'), true);
    app.reply({configured_theme:'default',color_mode:'light',server_now:new Date(epoch+2000).toISOString()});
    await app.refresh();
    assert.equal(app.classes.has('event-theme'), false);
});

test('preview is isolated from global theme polling', async () => {
    const app=page({preview:true});
    assert.equal(app.intervalCount, 0);
    assert.equal(app.listeners.has('pageshow'), false);
    assert.equal(app.classes.has('event-theme'), true);
});

test('dates farther than the browser timer limit do not activate early', () => {
    const future = 40*86400000;
    const app=page({starts_at:new Date(epoch+future).toISOString()});
    app.advance(2147483647);
    assert.equal(app.classes.has('event-theme'), false);
    app.advance(future-2147483647+10);
    assert.equal(app.classes.has('event-theme'), true);
});
