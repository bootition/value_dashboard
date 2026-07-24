import{J as e,V as t,_ as n,b as r,l as i}from"./runtime-core.esm-bundler-C-_igBqR.js";import{At as a,Ft as o,K as s,Nt as c,Tt as l,a as u,ft as d,i as f,kt as p,m,o as h,q as g,s as _,zt as v}from"./Scrollbar-CnuoQI0d.js";function y(e){let{opacityDisabled:t,heightTiny:n,heightSmall:r,heightMedium:i,heightLarge:a,heightHuge:o,primaryColor:s,fontSize:c}=e;return{fontSize:c,textColor:s,sizeTiny:n,sizeSmall:r,sizeMedium:i,sizeLarge:a,sizeHuge:o,color:s,opacitySpinning:t}}var b={name:`Spin`,common:f,self:y},x=p([p(`@keyframes spin-rotate`,`
 from {
 transform: rotate(0);
 }
 to {
 transform: rotate(360deg);
 }
 `),a(`spin-container`,`
 position: relative;
 `,[a(`spin-body`,`
 position: absolute;
 top: 50%;
 left: 50%;
 transform: translateX(-50%) translateY(-50%);
 `,[u()])]),a(`spin-body`,`
 display: inline-flex;
 align-items: center;
 justify-content: center;
 flex-direction: column;
 `),a(`spin`,`
 display: inline-flex;
 height: var(--n-size);
 width: var(--n-size);
 font-size: var(--n-size);
 color: var(--n-color);
 `,[c(`rotate`,`
 animation: spin-rotate 2s linear infinite;
 `)]),a(`spin-description`,`
 display: inline-block;
 font-size: var(--n-font-size);
 color: var(--n-text-color);
 transition: color .3s var(--n-bezier);
 margin-top: 8px;
 `),a(`spin-content`,`
 opacity: 1;
 transition: opacity .3s var(--n-bezier);
 pointer-events: all;
 `,[c(`spinning`,`
 user-select: none;
 -webkit-user-select: none;
 pointer-events: none;
 opacity: var(--n-opacity-spinning);
 `)])]),S={small:20,medium:18,large:16},C=n({name:`Spin`,props:Object.assign(Object.assign(Object.assign({},m.props),{contentClass:String,contentStyle:[Object,String],description:String,size:{type:[String,Number],default:`medium`},show:{type:Boolean,default:!0},rotate:{type:Boolean,default:!0},spinning:{type:Boolean,validator:()=>!0,default:void 0},delay:Number}),_),slots:Object,setup(n){let{mergedClsPrefixRef:r,inlineThemeDisabled:a}=g(n),c=m(`Spin`,`-spin`,x,b,n,r),u=i(()=>{let{size:e}=n,{common:{cubicBezierEaseInOut:t},self:r}=c.value,{opacitySpinning:i,color:a,textColor:s}=r;return{"--n-bezier":t,"--n-opacity-spinning":i,"--n-size":typeof e==`number`?l(e):r[o(`size`,e)],"--n-color":a,"--n-text-color":s}}),f=a?s(`spin`,i(()=>{let{size:e}=n;return typeof e==`number`?String(e):e[0]}),u,n):void 0,p=d(n,[`spinning`,`show`]),h=e(!1);return t(e=>{let t;if(p.value){let{delay:r}=n;if(r){t=window.setTimeout(()=>{h.value=!0},r),e(()=>{clearTimeout(t)});return}}h.value=p.value}),{mergedClsPrefix:r,active:h,mergedStrokeWidth:i(()=>{let{strokeWidth:e}=n;if(e!==void 0)return e;let{size:t}=n;return S[typeof t==`number`?`medium`:t]}),cssVars:a?void 0:u,themeClass:f?.themeClass,onRender:f?.onRender}},render(){var e;let{$slots:t,mergedClsPrefix:n,description:i}=this,a=t.icon&&this.rotate,o=(i||t.description)&&r(`div`,{class:`${n}-spin-description`},i||t.description?.call(t)),s=t.icon?r(`div`,{class:[`${n}-spin-body`,this.themeClass]},r(`div`,{class:[`${n}-spin`,a&&`${n}-spin--rotate`],style:t.default?``:this.cssVars},t.icon()),o):r(`div`,{class:[`${n}-spin-body`,this.themeClass]},r(h,{clsPrefix:n,style:t.default?``:this.cssVars,stroke:this.stroke,"stroke-width":this.mergedStrokeWidth,radius:this.radius,scale:this.scale,class:`${n}-spin`}),o);return(e=this.onRender)==null||e.call(this),t.default?r(`div`,{class:[`${n}-spin-container`,this.themeClass],style:this.cssVars},r(`div`,{class:[`${n}-spin-content`,this.active&&`${n}-spin-content--spinning`,this.contentClass],style:this.contentStyle},t),r(v,{name:`fade-in-transition`},{default:()=>this.active?s:null})):s}});export{C as t};