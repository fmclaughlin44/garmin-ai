// Minimal DOM, just enough to execute the dashboard's render pass and report
// the first real runtime error with a stack.
class El {
  constructor(tag, ns) {
    this.tagName = (tag || "").toUpperCase(); this.ns = ns || null;
    this.children = []; this.attributes = {}; this._text = "";
    this.style = new Proxy({}, { get: (t,k)=> t[k] ?? "", set: (t,k,v)=>{t[k]=v; return true;} });
    this.classList = {
      _s: new Set(),
      add:(...c)=>c.forEach(x=>this.classList._s.add(x)),
      remove:(...c)=>c.forEach(x=>this.classList._s.delete(x)),
      toggle:(c,on)=>{ on ? this.classList._s.add(c) : this.classList._s.delete(c); },
      contains:(c)=>this.classList._s.has(c),
    };
    this.dataset = {};
    this.hidden = false;
  }
  set className(v){ this._class = v; (v||"").split(/\s+/).forEach(c=>c&&this.classList._s.add(c)); }
  get className(){ return this._class || ""; }
  set textContent(v){ this._text = String(v); this.children = []; }
  get textContent(){ return this._text; }
  set innerHTML(v){ this._html = String(v); }
  get innerHTML(){ return this._html || ""; }
  setAttribute(k,v){
    this.attributes[k]=String(v);
    if (k==="id") { this.id=String(v); if (!(v in DOC._byId)) DOC._byId[v]=this; }
    if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-(\w)/g,(m,c)=>c.toUpperCase())]=String(v);
  }
  getAttribute(k){ return this.attributes[k] ?? null; }
  appendChild(c){ this.children.push(c); c.parentElement = this; return c; }
  prepend(c){ this.children.unshift(c); c.parentElement = this; return c; }
  replaceChildren(...c){ this.children = c; c.forEach(x=>x.parentElement=this); }
  addEventListener(){}
  removeEventListener(){}
  getBoundingClientRect(){ return {left:0,top:0,width:100,height:20,right:100,bottom:20}; }
  scrollIntoView(){}
  querySelectorAll(){ return []; }
  querySelector(){ return null; }
  get firstChild(){ return this.children[0] || null; }
  get parentElement(){ return this._parent || null; }
  set parentElement(p){ this._parent = p; }
}
const DOC = {
  _byId: {},
  title: "Training Dashboard",
  createElement: (t) => new El(t),
  createElementNS: (ns,t) => new El(t, ns),
  getElementById: (id) => DOC._byId[id] || null,
  querySelector: (sel) => DOC._byId["__page"] || new El("div"),
  querySelectorAll: () => [],
  createRange: () => ({ selectNodeContents(){} }),
  createTextNode: (v) => { const e = new El("#text"); e.textContent = String(v); return e; },
  addEventListener(){},
};
global.document = DOC;
global.window = {
  innerWidth: 1200, innerHeight: 900,
  matchMedia: () => ({ matches:false, addEventListener(){} }),
  getSelection: () => ({ removeAllRanges(){}, addRange(){} }),
  addEventListener(){},
};
global.navigator = { clipboard: { writeText: async()=>{} } };
global.history = { replaceState(){} };
global.location = { hash: "" };
global.IntersectionObserver = class { constructor(){} observe(){} disconnect(){} };
global.requestAnimationFrame = (f)=>f();
module.exports = { DOC, El };
