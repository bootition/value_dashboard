import{a as e,i as t,l as n,n as r,r as i,t as a,u as o}from"./axios-VgfTcLKy.js";import{B as s,M as c,Q as l,V as u,_ as d,b as f,d as p,f as m,g as h,h as g,k as _,l as v,p as y,pt as b,q as x,u as S}from"./runtime-core.esm-bundler-CGoGTtpb.js";import{At as C,Ft as w,K as T,Nt as E,Tt as D,a as O,ft as k,i as A,kt as j,m as M,o as N,q as P,s as F,zt as I}from"./Scrollbar-XF8rxoY5.js";function L(e){let{opacityDisabled:t,heightTiny:n,heightSmall:r,heightMedium:i,heightLarge:a,heightHuge:o,primaryColor:s,fontSize:c}=e;return{fontSize:c,textColor:s,sizeTiny:n,sizeSmall:r,sizeMedium:i,sizeLarge:a,sizeHuge:o,color:s,opacitySpinning:t}}var R={name:`Spin`,common:A,self:L},z=j([j(`@keyframes spin-rotate`,`
 from {
 transform: rotate(0);
 }
 to {
 transform: rotate(360deg);
 }
 `),C(`spin-container`,`
 position: relative;
 `,[C(`spin-body`,`
 position: absolute;
 top: 50%;
 left: 50%;
 transform: translateX(-50%) translateY(-50%);
 `,[O()])]),C(`spin-body`,`
 display: inline-flex;
 align-items: center;
 justify-content: center;
 flex-direction: column;
 `),C(`spin`,`
 display: inline-flex;
 height: var(--n-size);
 width: var(--n-size);
 font-size: var(--n-size);
 color: var(--n-color);
 `,[E(`rotate`,`
 animation: spin-rotate 2s linear infinite;
 `)]),C(`spin-description`,`
 display: inline-block;
 font-size: var(--n-font-size);
 color: var(--n-text-color);
 transition: color .3s var(--n-bezier);
 margin-top: 8px;
 `),C(`spin-content`,`
 opacity: 1;
 transition: opacity .3s var(--n-bezier);
 pointer-events: all;
 `,[E(`spinning`,`
 user-select: none;
 -webkit-user-select: none;
 pointer-events: none;
 opacity: var(--n-opacity-spinning);
 `)])]),B={small:20,medium:18,large:16},V=d({name:`Spin`,props:Object.assign(Object.assign(Object.assign({},M.props),{contentClass:String,contentStyle:[Object,String],description:String,size:{type:[String,Number],default:`medium`},show:{type:Boolean,default:!0},rotate:{type:Boolean,default:!0},spinning:{type:Boolean,validator:()=>!0,default:void 0},delay:Number}),F),slots:Object,setup(e){let{mergedClsPrefixRef:t,inlineThemeDisabled:n}=P(e),r=M(`Spin`,`-spin`,z,R,e,t),i=v(()=>{let{size:t}=e,{common:{cubicBezierEaseInOut:n},self:i}=r.value,{opacitySpinning:a,color:o,textColor:s}=i;return{"--n-bezier":n,"--n-opacity-spinning":a,"--n-size":typeof t==`number`?D(t):i[w(`size`,t)],"--n-color":o,"--n-text-color":s}}),a=n?T(`spin`,v(()=>{let{size:t}=e;return typeof t==`number`?String(t):t[0]}),i,e):void 0,o=k(e,[`spinning`,`show`]),c=x(!1);return s(t=>{let n;if(o.value){let{delay:r}=e;if(r){n=window.setTimeout(()=>{c.value=!0},r),t(()=>{clearTimeout(n)});return}}c.value=o.value}),{mergedClsPrefix:t,active:c,mergedStrokeWidth:v(()=>{let{strokeWidth:t}=e;if(t!==void 0)return t;let{size:n}=e;return B[typeof n==`number`?`medium`:n]}),cssVars:n?void 0:i,themeClass:a?.themeClass,onRender:a?.onRender}},render(){var e;let{$slots:t,mergedClsPrefix:n,description:r}=this,i=t.icon&&this.rotate,a=(r||t.description)&&f(`div`,{class:`${n}-spin-description`},r||t.description?.call(t)),o=t.icon?f(`div`,{class:[`${n}-spin-body`,this.themeClass]},f(`div`,{class:[`${n}-spin`,i&&`${n}-spin--rotate`],style:t.default?``:this.cssVars},t.icon()),a):f(`div`,{class:[`${n}-spin-body`,this.themeClass]},f(N,{clsPrefix:n,style:t.default?``:this.cssVars,stroke:this.stroke,"stroke-width":this.mergedStrokeWidth,radius:this.radius,scale:this.scale,class:`${n}-spin`}),a);return(e=this.onRender)==null||e.call(this),t.default?f(`div`,{class:[`${n}-spin-container`,this.themeClass],style:this.cssVars},f(`div`,{class:[`${n}-spin-content`,this.active&&`${n}-spin-content--spinning`,this.contentClass],style:this.contentStyle},t),f(I,{name:`fade-in-transition`},{default:()=>this.active?o:null})):o}}),H={key:0,style:{color:`red`}},U={key:1},W={style:{color:`#999`,"margin-bottom":`16px`}},G=d({__name:`DataStatusPage`,setup(s){let d=x(null),f=x(!0),v=x(``);async function C(){f.value=!0;try{let e=await a.get(`/api/data-status/summary`);d.value=e.data}catch(e){v.value=e.message||`加载失败`}finally{f.value=!1}}return _(C),(a,s)=>(c(),y(`div`,null,[s[1]||=S(`h2`,null,`数据状态`,-1),h(l(V),{show:f.value},{default:u(()=>[v.value?(c(),y(`div`,H,b(v.value),1)):d.value?(c(),y(`div`,U,[S(`p`,W,` 最近更新: `+b(d.value.last_update||`尚未初始化`),1),h(l(i),{cols:4,"x-gap":16,"y-gap":16},{default:u(()=>[h(l(t),null,{default:u(()=>[h(l(e),null,{default:u(()=>[h(l(r),{label:`股票总数`,value:d.value.stock_count},null,8,[`value`])]),_:1})]),_:1}),h(l(t),null,{default:u(()=>[h(l(e),null,{default:u(()=>[h(l(r),{label:`Raw价格覆盖`,value:d.value.price_raw_count},null,8,[`value`])]),_:1})]),_:1}),h(l(t),null,{default:u(()=>[h(l(e),null,{default:u(()=>[h(l(r),{label:`Qfq价格覆盖`,value:d.value.price_qfq_count},null,8,[`value`])]),_:1})]),_:1}),h(l(t),null,{default:u(()=>[h(l(e),null,{default:u(()=>[h(l(r),{label:`申万行业覆盖`,value:d.value.sw_industry_count},null,8,[`value`])]),_:1})]),_:1}),h(l(t),null,{default:u(()=>[h(l(e),null,{default:u(()=>[h(l(r),{label:`资产负债表`,value:d.value.balance_sheet_count},null,8,[`value`])]),_:1})]),_:1}),h(l(t),null,{default:u(()=>[h(l(e),null,{default:u(()=>[h(l(r),{label:`利润表`,value:d.value.income_statement_count},null,8,[`value`])]),_:1})]),_:1}),h(l(t),null,{default:u(()=>[h(l(e),null,{default:u(()=>[h(l(r),{label:`现金流量表`,value:d.value.cash_flow_count},null,8,[`value`])]),_:1})]),_:1}),h(l(t),null,{default:u(()=>[h(l(e),null,{default:u(()=>[h(l(r),{label:`待重试`,value:d.value.retry_count},{suffix:u(()=>[d.value.retry_count>0?(c(),p(l(n),{key:0,type:`warning`,size:`small`},{default:u(()=>[...s[0]||=[g(`需关注`,-1)]]),_:1})):m(``,!0)]),_:1},8,[`value`])]),_:1})]),_:1})]),_:1}),d.value.stock_count===0?(c(),p(l(e),{key:0,style:{"margin-top":`16px`}},{default:u(()=>[h(l(o),{description:`尚未初始化数据。请运行: python -m app.cli.main data init`})]),_:1})):m(``,!0)])):m(``,!0)]),_:1},8,[`show`])]))}});export{G as default};