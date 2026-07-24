import{a as e,i as t,l as n,n as r,r as i,t as a,u as o}from"./axios-nAMj50zR.js";import{$t as s,Cn as c,Et as l,Fn as u,Ft as d,G as f,Ht as p,It as m,K as h,Lt as g,Mn as _,Mt as v,Nn as y,Pt as b,Qt as x,R as S,Sn as C,V as w,Wt as T,Xt as E,Zt as D,_n as O,at as ee,bn as te,c as k,cn as A,dt as j,en as M,f as N,ft as ne,gn as P,i as F,in as I,it as L,jt as R,kn as z,ln as re,lt as ie,m as ae,mn as oe,nn as B,on as V,q as se,qt as ce,rn as H,rt as le,sn as U,st as W,tn as G,tt as K,wn as ue,wt as de,xn as q}from"./Scrollbar-Dr44NkBa.js";import{N as fe,P as pe,b as me,d as he,u as J,y as ge}from"./Popover-CQjj8Z1r.js";import{c as _e,i as ve,n as Y,o as ye,r as be,t as X}from"./Space-BdP1JExR.js";import{n as Z,r as xe,t as Se}from"./Spin-IFlUhrbS.js";import{t as Ce}from"./Add-B-UPIUxg.js";import{p as we,t as Te}from"./index-tcRB-HwK.js";var Ee=ge(`.v-x-scroll`,{overflow:`auto`,scrollbarWidth:`none`},[ge(`&::-webkit-scrollbar`,{width:0,height:0})]),De=I({name:`XScroll`,props:{disabled:Boolean,onScroll:Function},setup(){let e=z(null);function t(e){!(e.currentTarget.offsetWidth<e.currentTarget.scrollWidth)||e.deltaY===0||(e.currentTarget.scrollLeft+=e.deltaY+e.deltaX,e.preventDefault())}let n=ie();return Ee.mount({id:`vueuc/x-scroll`,head:!0,anchorMetaName:me,ssr:n}),Object.assign({selfRef:e,handleWheel:t},{scrollTo(...t){var n;(n=e.value)==null||n.scrollTo(...t)}})},render(){return V(`div`,{ref:`selfRef`,onScroll:this.onScroll,onWheel:this.disabled?void 0:this.handleWheel,class:`v-x-scroll`},this.$slots)}}),Oe=/\s/;function ke(e){for(var t=e.length;t--&&Oe.test(e.charAt(t)););return t}var Ae=/^\s+/;function je(e){return e&&e.slice(0,ke(e)+1).replace(Ae,``)}var Me=NaN,Ne=/^[-+]0x[0-9a-f]+$/i,Pe=/^0b[01]+$/i,Fe=/^0o[0-7]+$/i,Ie=parseInt;function Le(e){if(typeof e==`number`)return e;if(w(e))return Me;if(S(e)){var t=typeof e.valueOf==`function`?e.valueOf():e;e=S(t)?t+``:t}if(typeof e!=`string`)return e===0?e:+e;e=je(e);var n=Pe.test(e);return n||Fe.test(e)?Ie(e.slice(2),n?2:8):Ne.test(e)?Me:+e}var Re=function(){return f.Date.now()},ze=`Expected a function`,Be=Math.max,Ve=Math.min;function He(e,t,n){var r,i,a,o,s,c,l=0,u=!1,d=!1,f=!0;if(typeof e!=`function`)throw TypeError(ze);t=Le(t)||0,S(n)&&(u=!!n.leading,d=`maxWait`in n,a=d?Be(Le(n.maxWait)||0,t):a,f=`trailing`in n?!!n.trailing:f);function p(t){var n=r,a=i;return r=i=void 0,l=t,o=e.apply(a,n),o}function m(e){return l=e,s=setTimeout(_,t),u?p(e):o}function h(e){var n=e-c,r=e-l,i=t-n;return d?Ve(i,a-r):i}function g(e){var n=e-c,r=e-l;return c===void 0||n>=t||n<0||d&&r>=a}function _(){var e=Re();if(g(e))return v(e);s=setTimeout(_,h(e))}function v(e){return s=void 0,f&&r?p(e):(r=i=void 0,o)}function y(){s!==void 0&&clearTimeout(s),l=0,r=c=i=s=void 0}function b(){return s===void 0?o:v(Re())}function x(){var e=Re(),n=g(e);if(r=arguments,i=this,c=e,n){if(s===void 0)return m(c);if(d)return clearTimeout(s),s=setTimeout(_,t),p(c)}return s===void 0&&(s=setTimeout(_,t)),o}return x.cancel=y,x.flush=b,x}var Ue=`Expected a function`;function We(e,t,n){var r=!0,i=!0;if(typeof e!=`function`)throw TypeError(Ue);return S(n)&&(r=`leading`in n?!!n.leading:r,i=`trailing`in n?!!n.trailing:i),He(e,t,{leading:r,maxWait:t,trailing:i})}var Q=I({name:`RadioButton`,props:be,setup:ve,render(){let{mergedClsPrefix:e}=this;return V(`label`,{class:[`${e}-radio-button`,this.mergedDisabled&&`${e}-radio-button--disabled`,this.renderSafeChecked&&`${e}-radio-button--checked`,this.focus&&[`${e}-radio-button--focus`]]},V(`input`,{ref:`inputRef`,type:`radio`,class:`${e}-radio-input`,value:this.value,name:this.mergedName,checked:this.renderSafeChecked,disabled:this.mergedDisabled,onChange:this.handleRadioInputChange,onFocus:this.handleRadioInputFocus,onBlur:this.handleRadioInputBlur}),V(`div`,{class:`${e}-radio-button__state-border`}),K(this.$slots.default,t=>!t&&!this.label?null:V(`div`,{ref:`labelRef`,class:`${e}-radio__label`},t||this.label)))}}),Ge={tabFontSizeSmall:`14px`,tabFontSizeMedium:`14px`,tabFontSizeLarge:`16px`,tabGapSmallLine:`36px`,tabGapMediumLine:`36px`,tabGapLargeLine:`36px`,tabGapSmallLineVertical:`8px`,tabGapMediumLineVertical:`8px`,tabGapLargeLineVertical:`8px`,tabPaddingSmallLine:`6px 0`,tabPaddingMediumLine:`10px 0`,tabPaddingLargeLine:`14px 0`,tabPaddingVerticalSmallLine:`6px 12px`,tabPaddingVerticalMediumLine:`8px 16px`,tabPaddingVerticalLargeLine:`10px 20px`,tabGapSmallBar:`36px`,tabGapMediumBar:`36px`,tabGapLargeBar:`36px`,tabGapSmallBarVertical:`8px`,tabGapMediumBarVertical:`8px`,tabGapLargeBarVertical:`8px`,tabPaddingSmallBar:`4px 0`,tabPaddingMediumBar:`6px 0`,tabPaddingLargeBar:`10px 0`,tabPaddingVerticalSmallBar:`6px 12px`,tabPaddingVerticalMediumBar:`8px 16px`,tabPaddingVerticalLargeBar:`10px 20px`,tabGapSmallCard:`4px`,tabGapMediumCard:`4px`,tabGapLargeCard:`4px`,tabGapSmallCardVertical:`4px`,tabGapMediumCardVertical:`4px`,tabGapLargeCardVertical:`4px`,tabPaddingSmallCard:`8px 16px`,tabPaddingMediumCard:`10px 20px`,tabPaddingLargeCard:`12px 24px`,tabPaddingSmallSegment:`4px 0`,tabPaddingMediumSegment:`6px 0`,tabPaddingLargeSegment:`8px 0`,tabPaddingVerticalLargeSegment:`0 8px`,tabPaddingVerticalSmallCard:`8px 12px`,tabPaddingVerticalMediumCard:`10px 16px`,tabPaddingVerticalLargeCard:`12px 20px`,tabPaddingVerticalSmallSegment:`0 4px`,tabPaddingVerticalMediumSegment:`0 6px`,tabGapSmallSegment:`0`,tabGapMediumSegment:`0`,tabGapLargeSegment:`0`,tabGapSmallSegmentVertical:`0`,tabGapMediumSegmentVertical:`0`,tabGapLargeSegmentVertical:`0`,panePaddingSmall:`8px 0 0 0`,panePaddingMedium:`12px 0 0 0`,panePaddingLarge:`16px 0 0 0`,closeSize:`18px`,closeIconSize:`14px`};function Ke(e){let{textColor2:t,primaryColor:n,textColorDisabled:r,closeIconColor:i,closeIconColorHover:a,closeIconColorPressed:o,closeColorHover:s,closeColorPressed:c,tabColor:l,baseColor:u,dividerColor:d,fontWeight:f,textColor1:p,borderRadius:m,fontSize:h,fontWeightStrong:g}=e;return Object.assign(Object.assign({},Ge),{colorSegment:l,tabFontSizeCard:h,tabTextColorLine:p,tabTextColorActiveLine:n,tabTextColorHoverLine:n,tabTextColorDisabledLine:r,tabTextColorSegment:p,tabTextColorActiveSegment:t,tabTextColorHoverSegment:t,tabTextColorDisabledSegment:r,tabTextColorBar:p,tabTextColorActiveBar:n,tabTextColorHoverBar:n,tabTextColorDisabledBar:r,tabTextColorCard:p,tabTextColorHoverCard:p,tabTextColorActiveCard:n,tabTextColorDisabledCard:r,barColor:n,closeIconColor:i,closeIconColorHover:a,closeIconColorPressed:o,closeColorHover:s,closeColorPressed:c,closeBorderRadius:m,tabColor:l,tabColorSegment:u,tabBorderColor:d,tabFontWeightActive:f,tabFontWeight:f,tabBorderRadius:m,paneTextColor:t,fontWeightStrong:g})}var qe={name:`Tabs`,common:F,self:Ke},Je=j(`n-tabs`),Ye={tab:[String,Number,Object,Function],name:{type:[String,Number],required:!0},disabled:Boolean,displayDirective:{type:String,default:`if`},closable:{type:Boolean,default:void 0},tabProps:Object,label:[String,Number,Object,Function]},$=I({__TAB_PANE__:!0,name:`TabPane`,alias:[`TabPanel`],props:Ye,slots:Object,setup(e){let t=U(Je,null);return t||ee(`tab-pane`,"`n-tab-pane` must be placed inside `n-tabs`."),{style:t.paneStyleRef,class:t.paneClassRef,mergedClsPrefix:t.mergedClsPrefixRef}},render(){return V(`div`,{class:[`${this.mergedClsPrefix}-tab-pane`,this.class],style:this.style},this.$slots)}}),Xe=I({__TAB__:!0,inheritAttrs:!1,name:`Tab`,props:Object.assign({internalLeftPadded:Boolean,internalAddable:Boolean,internalCreatedByPane:Boolean},he(Ye,[`displayDirective`])),setup(e){let{mergedClsPrefixRef:t,valueRef:n,typeRef:r,closableRef:i,tabStyleRef:a,addTabStyleRef:o,tabClassRef:s,addTabClassRef:c,tabChangeIdRef:l,onBeforeLeaveRef:u,triggerRef:d,handleAdd:f,activateTab:p,handleClose:m}=U(Je);return{trigger:d,mergedClosable:D(()=>{if(e.internalAddable)return!1;let{closable:t}=e;return t===void 0?i.value:t}),style:a,addStyle:o,tabClass:s,addTabClass:c,clsPrefix:t,value:n,type:r,handleClose(t){t.stopPropagation(),!e.disabled&&m(e.name)},activateTab(){if(e.disabled)return;if(e.internalAddable){f();return}let{name:t}=e,r=++l.id;if(t!==n.value){let{value:i}=u;i?Promise.resolve(i(e.name,n.value)).then(e=>{e&&l.id===r&&p(t)}):p(t)}}}},render(){let{internalAddable:e,clsPrefix:t,name:n,disabled:r,label:i,tab:a,value:o,mergedClosable:s,trigger:c,$slots:{default:l}}=this,u=i??a;return V(`div`,{class:`${t}-tabs-tab-wrapper`},this.internalLeftPadded?V(`div`,{class:`${t}-tabs-tab-pad`}):null,V(`div`,Object.assign({key:n,"data-name":n,"data-disabled":r?!0:void 0},A({class:[`${t}-tabs-tab`,o===n&&`${t}-tabs-tab--active`,r&&`${t}-tabs-tab--disabled`,s&&`${t}-tabs-tab--closable`,e&&`${t}-tabs-tab--addable`,e?this.addTabClass:this.tabClass],onClick:c===`click`?this.activateTab:void 0,onMouseenter:c===`hover`?this.activateTab:void 0,style:e?this.addStyle:this.style},this.internalCreatedByPane?this.tabProps||{}:this.$attrs)),V(`span`,{class:`${t}-tabs-tab__label`},e?V(ce,null,V(`div`,{class:`${t}-tabs-tab__height-placeholder`},`\xA0`),V(N,{clsPrefix:t},{default:()=>V(Ce,null)})):l?l():typeof u==`object`?u:J(u??n)),s&&this.type===`card`?V(k,{clsPrefix:t,class:`${t}-tabs-tab__close`,onClick:this.handleClose,disabled:r}):null))}}),Ze=v(`tabs`,`
 box-sizing: border-box;
 width: 100%;
 display: flex;
 flex-direction: column;
 transition:
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier);
`,[d(`segment-type`,[v(`tabs-rail`,[R(`&.transition-disabled`,[v(`tabs-capsule`,`
 transition: none;
 `)])])]),d(`top`,[v(`tab-pane`,`
 padding: var(--n-pane-padding-top) var(--n-pane-padding-right) var(--n-pane-padding-bottom) var(--n-pane-padding-left);
 `)]),d(`left`,[v(`tab-pane`,`
 padding: var(--n-pane-padding-right) var(--n-pane-padding-bottom) var(--n-pane-padding-left) var(--n-pane-padding-top);
 `)]),d(`left, right`,`
 flex-direction: row;
 `,[v(`tabs-bar`,`
 width: 2px;
 right: 0;
 transition:
 top .2s var(--n-bezier),
 max-height .2s var(--n-bezier),
 background-color .3s var(--n-bezier);
 `),v(`tabs-tab`,`
 padding: var(--n-tab-padding-vertical); 
 `)]),d(`right`,`
 flex-direction: row-reverse;
 `,[v(`tab-pane`,`
 padding: var(--n-pane-padding-left) var(--n-pane-padding-top) var(--n-pane-padding-right) var(--n-pane-padding-bottom);
 `),v(`tabs-bar`,`
 left: 0;
 `)]),d(`bottom`,`
 flex-direction: column-reverse;
 justify-content: flex-end;
 `,[v(`tab-pane`,`
 padding: var(--n-pane-padding-bottom) var(--n-pane-padding-right) var(--n-pane-padding-top) var(--n-pane-padding-left);
 `),v(`tabs-bar`,`
 top: 0;
 `)]),v(`tabs-rail`,`
 position: relative;
 padding: 3px;
 border-radius: var(--n-tab-border-radius);
 width: 100%;
 background-color: var(--n-color-segment);
 transition: background-color .3s var(--n-bezier);
 display: flex;
 align-items: center;
 `,[v(`tabs-capsule`,`
 border-radius: var(--n-tab-border-radius);
 position: absolute;
 pointer-events: none;
 background-color: var(--n-tab-color-segment);
 box-shadow: 0 1px 3px 0 rgba(0, 0, 0, .08);
 transition: transform 0.3s var(--n-bezier);
 `),v(`tabs-tab-wrapper`,`
 flex-basis: 0;
 flex-grow: 1;
 display: flex;
 align-items: center;
 justify-content: center;
 `,[v(`tabs-tab`,`
 overflow: hidden;
 border-radius: var(--n-tab-border-radius);
 width: 100%;
 display: flex;
 align-items: center;
 justify-content: center;
 `,[d(`active`,`
 font-weight: var(--n-font-weight-strong);
 color: var(--n-tab-text-color-active);
 `),R(`&:hover`,`
 color: var(--n-tab-text-color-hover);
 `)])])]),d(`flex`,[v(`tabs-nav`,`
 width: 100%;
 position: relative;
 `,[v(`tabs-wrapper`,`
 width: 100%;
 `,[v(`tabs-tab`,`
 margin-right: 0;
 `)])])]),v(`tabs-nav`,`
 box-sizing: border-box;
 line-height: 1.5;
 display: flex;
 transition: border-color .3s var(--n-bezier);
 `,[b(`prefix, suffix`,`
 display: flex;
 align-items: center;
 `),b(`prefix`,`padding-right: 16px;`),b(`suffix`,`padding-left: 16px;`)]),d(`top, bottom`,[R(`>`,[v(`tabs-nav`,[v(`tabs-nav-scroll-wrapper`,[R(`&::before`,`
 top: 0;
 bottom: 0;
 left: 0;
 width: 20px;
 `),R(`&::after`,`
 top: 0;
 bottom: 0;
 right: 0;
 width: 20px;
 `),d(`shadow-start`,[R(`&::before`,`
 box-shadow: inset 10px 0 8px -8px rgba(0, 0, 0, .12);
 `)]),d(`shadow-end`,[R(`&::after`,`
 box-shadow: inset -10px 0 8px -8px rgba(0, 0, 0, .12);
 `)])])])])]),d(`left, right`,[v(`tabs-nav-scroll-content`,`
 flex-direction: column;
 `),R(`>`,[v(`tabs-nav`,[v(`tabs-nav-scroll-wrapper`,[R(`&::before`,`
 top: 0;
 left: 0;
 right: 0;
 height: 20px;
 `),R(`&::after`,`
 bottom: 0;
 left: 0;
 right: 0;
 height: 20px;
 `),d(`shadow-start`,[R(`&::before`,`
 box-shadow: inset 0 10px 8px -8px rgba(0, 0, 0, .12);
 `)]),d(`shadow-end`,[R(`&::after`,`
 box-shadow: inset 0 -10px 8px -8px rgba(0, 0, 0, .12);
 `)])])])])]),v(`tabs-nav-scroll-wrapper`,`
 flex: 1;
 position: relative;
 overflow: hidden;
 `,[v(`tabs-nav-y-scroll`,`
 height: 100%;
 width: 100%;
 overflow-y: auto; 
 scrollbar-width: none;
 `,[R(`&::-webkit-scrollbar, &::-webkit-scrollbar-track-piece, &::-webkit-scrollbar-thumb`,`
 width: 0;
 height: 0;
 display: none;
 `)]),R(`&::before, &::after`,`
 transition: box-shadow .3s var(--n-bezier);
 pointer-events: none;
 content: "";
 position: absolute;
 z-index: 1;
 `)]),v(`tabs-nav-scroll-content`,`
 display: flex;
 position: relative;
 min-width: 100%;
 min-height: 100%;
 width: fit-content;
 box-sizing: border-box;
 `),v(`tabs-wrapper`,`
 display: inline-flex;
 flex-wrap: nowrap;
 position: relative;
 `),v(`tabs-tab-wrapper`,`
 display: flex;
 flex-wrap: nowrap;
 flex-shrink: 0;
 flex-grow: 0;
 `),v(`tabs-tab`,`
 cursor: pointer;
 white-space: nowrap;
 flex-wrap: nowrap;
 display: inline-flex;
 align-items: center;
 color: var(--n-tab-text-color);
 font-size: var(--n-tab-font-size);
 background-clip: padding-box;
 padding: var(--n-tab-padding);
 transition:
 box-shadow .3s var(--n-bezier),
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier);
 `,[d(`disabled`,{cursor:`not-allowed`}),b(`close`,`
 margin-left: 6px;
 transition:
 background-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 `),b(`label`,`
 display: flex;
 align-items: center;
 z-index: 1;
 `)]),v(`tabs-bar`,`
 position: absolute;
 bottom: 0;
 height: 2px;
 border-radius: 1px;
 background-color: var(--n-bar-color);
 transition:
 left .2s var(--n-bezier),
 max-width .2s var(--n-bezier),
 opacity .3s var(--n-bezier),
 background-color .3s var(--n-bezier);
 `,[R(`&.transition-disabled`,`
 transition: none;
 `),d(`disabled`,`
 background-color: var(--n-tab-text-color-disabled)
 `)]),v(`tabs-pane-wrapper`,`
 position: relative;
 overflow: hidden;
 transition: max-height .2s var(--n-bezier);
 `),v(`tab-pane`,`
 color: var(--n-pane-text-color);
 width: 100%;
 transition:
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 opacity .2s var(--n-bezier);
 left: 0;
 right: 0;
 top: 0;
 `,[R(`&.next-transition-leave-active, &.prev-transition-leave-active, &.next-transition-enter-active, &.prev-transition-enter-active`,`
 transition:
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 transform .2s var(--n-bezier),
 opacity .2s var(--n-bezier);
 `),R(`&.next-transition-leave-active, &.prev-transition-leave-active`,`
 position: absolute;
 `),R(`&.next-transition-enter-from, &.prev-transition-leave-to`,`
 transform: translateX(32px);
 opacity: 0;
 `),R(`&.next-transition-leave-to, &.prev-transition-enter-from`,`
 transform: translateX(-32px);
 opacity: 0;
 `),R(`&.next-transition-leave-from, &.next-transition-enter-to, &.prev-transition-leave-from, &.prev-transition-enter-to`,`
 transform: translateX(0);
 opacity: 1;
 `)]),v(`tabs-tab-pad`,`
 box-sizing: border-box;
 width: var(--n-tab-gap);
 flex-grow: 0;
 flex-shrink: 0;
 `),d(`line-type, bar-type`,[v(`tabs-tab`,`
 font-weight: var(--n-tab-font-weight);
 box-sizing: border-box;
 vertical-align: bottom;
 `,[R(`&:hover`,{color:`var(--n-tab-text-color-hover)`}),d(`active`,`
 color: var(--n-tab-text-color-active);
 font-weight: var(--n-tab-font-weight-active);
 `),d(`disabled`,{color:`var(--n-tab-text-color-disabled)`})])]),v(`tabs-nav`,[d(`line-type`,[d(`top`,[b(`prefix, suffix`,`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),v(`tabs-nav-scroll-content`,`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),v(`tabs-bar`,`
 bottom: -1px;
 `)]),d(`left`,[b(`prefix, suffix`,`
 border-right: 1px solid var(--n-tab-border-color);
 `),v(`tabs-nav-scroll-content`,`
 border-right: 1px solid var(--n-tab-border-color);
 `),v(`tabs-bar`,`
 right: -1px;
 `)]),d(`right`,[b(`prefix, suffix`,`
 border-left: 1px solid var(--n-tab-border-color);
 `),v(`tabs-nav-scroll-content`,`
 border-left: 1px solid var(--n-tab-border-color);
 `),v(`tabs-bar`,`
 left: -1px;
 `)]),d(`bottom`,[b(`prefix, suffix`,`
 border-top: 1px solid var(--n-tab-border-color);
 `),v(`tabs-nav-scroll-content`,`
 border-top: 1px solid var(--n-tab-border-color);
 `),v(`tabs-bar`,`
 top: -1px;
 `)]),b(`prefix, suffix`,`
 transition: border-color .3s var(--n-bezier);
 `),v(`tabs-nav-scroll-content`,`
 transition: border-color .3s var(--n-bezier);
 `),v(`tabs-bar`,`
 border-radius: 0;
 `)]),d(`card-type`,[b(`prefix, suffix`,`
 transition: border-color .3s var(--n-bezier);
 `),v(`tabs-pad`,`
 flex-grow: 1;
 transition: border-color .3s var(--n-bezier);
 `),v(`tabs-tab-pad`,`
 transition: border-color .3s var(--n-bezier);
 `),v(`tabs-tab`,`
 font-weight: var(--n-tab-font-weight);
 border: 1px solid var(--n-tab-border-color);
 background-color: var(--n-tab-color);
 box-sizing: border-box;
 position: relative;
 vertical-align: bottom;
 display: flex;
 justify-content: space-between;
 font-size: var(--n-tab-font-size);
 color: var(--n-tab-text-color);
 `,[d(`addable`,`
 padding-left: 8px;
 padding-right: 8px;
 font-size: 16px;
 justify-content: center;
 `,[b(`height-placeholder`,`
 width: 0;
 font-size: var(--n-tab-font-size);
 `),m(`disabled`,[R(`&:hover`,`
 color: var(--n-tab-text-color-hover);
 `)])]),d(`closable`,`padding-right: 8px;`),d(`active`,`
 background-color: #0000;
 font-weight: var(--n-tab-font-weight-active);
 color: var(--n-tab-text-color-active);
 `),d(`disabled`,`color: var(--n-tab-text-color-disabled);`)])]),d(`left, right`,`
 flex-direction: column; 
 `,[b(`prefix, suffix`,`
 padding: var(--n-tab-padding-vertical);
 `),v(`tabs-wrapper`,`
 flex-direction: column;
 `),v(`tabs-tab-wrapper`,`
 flex-direction: column;
 `,[v(`tabs-tab-pad`,`
 height: var(--n-tab-gap-vertical);
 width: 100%;
 `)])]),d(`top`,[d(`card-type`,[v(`tabs-scroll-padding`,`border-bottom: 1px solid var(--n-tab-border-color);`),b(`prefix, suffix`,`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),v(`tabs-tab`,`
 border-top-left-radius: var(--n-tab-border-radius);
 border-top-right-radius: var(--n-tab-border-radius);
 `,[d(`active`,`
 border-bottom: 1px solid #0000;
 `)]),v(`tabs-tab-pad`,`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),v(`tabs-pad`,`
 border-bottom: 1px solid var(--n-tab-border-color);
 `)])]),d(`left`,[d(`card-type`,[v(`tabs-scroll-padding`,`border-right: 1px solid var(--n-tab-border-color);`),b(`prefix, suffix`,`
 border-right: 1px solid var(--n-tab-border-color);
 `),v(`tabs-tab`,`
 border-top-left-radius: var(--n-tab-border-radius);
 border-bottom-left-radius: var(--n-tab-border-radius);
 `,[d(`active`,`
 border-right: 1px solid #0000;
 `)]),v(`tabs-tab-pad`,`
 border-right: 1px solid var(--n-tab-border-color);
 `),v(`tabs-pad`,`
 border-right: 1px solid var(--n-tab-border-color);
 `)])]),d(`right`,[d(`card-type`,[v(`tabs-scroll-padding`,`border-left: 1px solid var(--n-tab-border-color);`),b(`prefix, suffix`,`
 border-left: 1px solid var(--n-tab-border-color);
 `),v(`tabs-tab`,`
 border-top-right-radius: var(--n-tab-border-radius);
 border-bottom-right-radius: var(--n-tab-border-radius);
 `,[d(`active`,`
 border-left: 1px solid #0000;
 `)]),v(`tabs-tab-pad`,`
 border-left: 1px solid var(--n-tab-border-color);
 `),v(`tabs-pad`,`
 border-left: 1px solid var(--n-tab-border-color);
 `)])]),d(`bottom`,[d(`card-type`,[v(`tabs-scroll-padding`,`border-top: 1px solid var(--n-tab-border-color);`),b(`prefix, suffix`,`
 border-top: 1px solid var(--n-tab-border-color);
 `),v(`tabs-tab`,`
 border-bottom-left-radius: var(--n-tab-border-radius);
 border-bottom-right-radius: var(--n-tab-border-radius);
 `,[d(`active`,`
 border-top: 1px solid #0000;
 `)]),v(`tabs-tab-pad`,`
 border-top: 1px solid var(--n-tab-border-color);
 `),v(`tabs-pad`,`
 border-top: 1px solid var(--n-tab-border-color);
 `)])])])]),Qe=We,$e=I({name:`Tabs`,props:Object.assign(Object.assign({},ae.props),{value:[String,Number],defaultValue:[String,Number],trigger:{type:String,default:`click`},type:{type:String,default:`bar`},closable:Boolean,justifyContent:String,size:String,placement:{type:String,default:`top`},tabStyle:[String,Object],tabClass:String,addTabStyle:[String,Object],addTabClass:String,barWidth:Number,paneClass:String,paneStyle:[String,Object],paneWrapperClass:String,paneWrapperStyle:[String,Object],addable:[Boolean,Object],tabsPadding:{type:Number,default:0},animated:Boolean,onBeforeLeave:Function,onAdd:Function,"onUpdate:value":[Function,Array],onUpdateValue:[Function,Array],onClose:[Function,Array],labelSize:String,activeName:[String,Number],onActiveNameChange:[Function,Array]}),slots:Object,setup(e,{slots:t}){let{mergedClsPrefixRef:n,inlineThemeDisabled:r,mergedComponentPropsRef:i}=se(e),a=ae(`Tabs`,`-tabs`,Ze,qe,e,n),o=z(null),s=z(null),c=z(null),u=z(null),d=z(null),f=z(null),p=z(!0),m=z(!0),v=ne(e,[`labelSize`,`size`]),y=D(()=>v.value?v.value:i?.value?.Tabs?.size||`medium`),b=ne(e,[`activeName`,`value`]),x=z(b.value??e.defaultValue??(t.default?le(t.default())[0]?.props?.name:null)),S=fe(b,x),w={id:0},T=D(()=>{if(!(!e.justifyContent||e.type===`card`))return{display:`flex`,justifyContent:e.justifyContent}});q(S,()=>{w.id=0,A(),j()});function E(){let{value:e}=S;return e===null?null:o.value?.querySelector(`[data-name="${e}"]`)}function ee(t){if(e.type===`card`)return;let{value:r}=s;if(!r)return;let i=r.style.opacity===`0`;if(t){let a=`${n.value}-tabs-bar--disabled`,{barWidth:o,placement:s}=e;if(t.dataset.disabled===`true`?r.classList.add(a):r.classList.remove(a),[`top`,`bottom`].includes(s)){if(k([`top`,`maxHeight`,`height`]),typeof o==`number`&&t.offsetWidth>=o){let e=Math.floor((t.offsetWidth-o)/2)+t.offsetLeft;r.style.left=`${e}px`,r.style.maxWidth=`${o}px`}else r.style.left=`${t.offsetLeft}px`,r.style.maxWidth=`${t.offsetWidth}px`;r.style.width=`8192px`,i&&(r.style.transition=`none`),r.offsetWidth,i&&(r.style.transition=``,r.style.opacity=`1`)}else{if(k([`left`,`maxWidth`,`width`]),typeof o==`number`&&t.offsetHeight>=o){let e=Math.floor((t.offsetHeight-o)/2)+t.offsetTop;r.style.top=`${e}px`,r.style.maxHeight=`${o}px`}else r.style.top=`${t.offsetTop}px`,r.style.maxHeight=`${t.offsetHeight}px`;r.style.height=`8192px`,i&&(r.style.transition=`none`),r.offsetHeight,i&&(r.style.transition=``,r.style.opacity=`1`)}}}function te(){if(e.type===`card`)return;let{value:t}=s;t&&(t.style.opacity=`0`)}function k(e){let{value:t}=s;if(t)for(let n of e)t.style[n]=``}function A(){if(e.type===`card`)return;let t=E();t?ee(t):te()}function j(){let e=d.value?.$el;if(!e)return;let t=E();if(!t)return;let{scrollLeft:n,offsetWidth:r}=e,{offsetLeft:i,offsetWidth:a}=t;n>i?e.scrollTo({top:0,left:i,behavior:`smooth`}):i+a>n+r&&e.scrollTo({top:0,left:i+a-r,behavior:`smooth`})}let M=z(null),N=0,P=null;function F(e){let t=M.value;if(t){N=e.getBoundingClientRect().height;let n=`${N}px`,r=()=>{t.style.height=n,t.style.maxHeight=n};P?(r(),P(),P=null):P=r}}function I(e){let t=M.value;if(t){let n=e.getBoundingClientRect().height,r=()=>{document.body.offsetHeight,t.style.maxHeight=`${n}px`,t.style.height=`${Math.max(N,n)}px`};P?(P(),P=null,r()):P=r}}function R(){let t=M.value;if(t){t.style.maxHeight=``,t.style.height=``;let{paneWrapperStyle:n}=e;if(typeof n==`string`)t.style.cssText=n;else if(n){let{maxHeight:e,height:r}=n;e!==void 0&&(t.style.maxHeight=e),r!==void 0&&(t.style.height=r)}}}let ie={value:[]},B=z(`next`);function V(e){let t=S.value,n=`next`;for(let r of ie.value){if(r===t)break;if(r===e){n=`prev`;break}}B.value=n,ce(e)}function ce(t){let{onActiveNameChange:n,onUpdateValue:r,"onUpdate:value":i}=e;n&&L(n,t),r&&L(r,t),i&&L(i,t),x.value=t}function H(t){let{onClose:n}=e;n&&L(n,t)}let U=!0;function W(){let{value:e}=s;if(!e)return;U||=!1;let t=`transition-disabled`;e.classList.add(t),A(),e.classList.remove(t)}let G=z(null);function K({transitionDisabled:e}){let t=o.value;if(!t)return;e&&t.classList.add(`transition-disabled`);let n=E();n&&G.value&&(G.value.style.width=`${n.offsetWidth}px`,G.value.style.height=`${n.offsetHeight}px`,G.value.style.transform=`translateX(${n.offsetLeft-de(getComputedStyle(t).paddingLeft)}px)`,e&&G.value.offsetWidth),e&&t.classList.remove(`transition-disabled`)}q([S],()=>{e.type===`segment`&&re(()=>{K({transitionDisabled:!1})})}),oe(()=>{e.type===`segment`&&K({transitionDisabled:!0})});let ue=0;function me(t){if(t.contentRect.width===0&&t.contentRect.height===0||ue===t.contentRect.width)return;ue=t.contentRect.width;let{type:n}=e;if((n===`line`||n===`bar`)&&(U||e.justifyContent?.startsWith(`space`))&&W(),n!==`segment`){let{placement:t}=e;Y((t===`top`||t===`bottom`?d.value?.$el:f.value)||null)}}let he=Qe(me,64);q([()=>e.justifyContent,()=>e.size],()=>{re(()=>{let{type:t}=e;(t===`line`||t===`bar`)&&W()})});let J=z(!1);function ge(t){let{target:n,contentRect:{width:r,height:i}}=t,a=n.parentElement.parentElement.offsetWidth,o=n.parentElement.parentElement.offsetHeight,{placement:s}=e;if(!J.value)s===`top`||s===`bottom`?a<r&&(J.value=!0):o<i&&(J.value=!0);else{let{value:e}=u;if(!e)return;s===`top`||s===`bottom`?a-r>e.$el.offsetWidth&&(J.value=!1):o-i>e.$el.offsetHeight&&(J.value=!1)}Y(d.value?.$el||null)}let _e=Qe(ge,64);function ve(){let{onAdd:t}=e;t&&t(),re(()=>{let e=E(),{value:t}=d;!e||!t||t.scrollTo({left:e.offsetLeft,top:0,behavior:`smooth`})})}function Y(t){if(!t)return;let{placement:n}=e;if(n===`top`||n===`bottom`){let{scrollLeft:e,scrollWidth:n,offsetWidth:r}=t;p.value=e<=0,m.value=e+r>=n}else{let{scrollTop:e,scrollHeight:n,offsetHeight:r}=t;p.value=e<=0,m.value=e+r>=n}}let ye=Qe(e=>{Y(e.target)},64);O(Je,{triggerRef:_(e,`trigger`),tabStyleRef:_(e,`tabStyle`),tabClassRef:_(e,`tabClass`),addTabStyleRef:_(e,`addTabStyle`),addTabClassRef:_(e,`addTabClass`),paneClassRef:_(e,`paneClass`),paneStyleRef:_(e,`paneStyle`),mergedClsPrefixRef:n,typeRef:_(e,`type`),closableRef:_(e,`closable`),valueRef:S,tabChangeIdRef:w,onBeforeLeaveRef:_(e,`onBeforeLeave`),activateTab:V,handleClose:H,handleAdd:ve}),pe(()=>{A(),j()}),C(()=>{let{value:e}=c;if(!e)return;let{value:t}=n,r=`${t}-tabs-nav-scroll-wrapper--shadow-start`,i=`${t}-tabs-nav-scroll-wrapper--shadow-end`;p.value?e.classList.remove(r):e.classList.add(r),m.value?e.classList.remove(i):e.classList.add(i)});let be={syncBarPosition:()=>{A()}},X=()=>{K({transitionDisabled:!0})},Z=D(()=>{let{value:t}=y,{type:n}=e,r=`${t}${{card:`Card`,bar:`Bar`,line:`Line`,segment:`Segment`}[n]}`,{self:{barColor:i,closeIconColor:o,closeIconColorHover:s,closeIconColorPressed:c,tabColor:u,tabBorderColor:d,paneTextColor:f,tabFontWeight:p,tabBorderRadius:m,tabFontWeightActive:h,colorSegment:_,fontWeightStrong:v,tabColorSegment:b,closeSize:x,closeIconSize:S,closeColorHover:C,closeColorPressed:w,closeBorderRadius:T,[g(`panePadding`,t)]:E,[g(`tabPadding`,r)]:D,[g(`tabPaddingVertical`,r)]:O,[g(`tabGap`,r)]:ee,[g(`tabGap`,`${r}Vertical`)]:te,[g(`tabTextColor`,n)]:k,[g(`tabTextColorActive`,n)]:A,[g(`tabTextColorHover`,n)]:j,[g(`tabTextColorDisabled`,n)]:M,[g(`tabFontSize`,t)]:N},common:{cubicBezierEaseInOut:ne}}=a.value;return{"--n-bezier":ne,"--n-color-segment":_,"--n-bar-color":i,"--n-tab-font-size":N,"--n-tab-text-color":k,"--n-tab-text-color-active":A,"--n-tab-text-color-disabled":M,"--n-tab-text-color-hover":j,"--n-pane-text-color":f,"--n-tab-border-color":d,"--n-tab-border-radius":m,"--n-close-size":x,"--n-close-icon-size":S,"--n-close-color-hover":C,"--n-close-color-pressed":w,"--n-close-border-radius":T,"--n-close-icon-color":o,"--n-close-icon-color-hover":s,"--n-close-icon-color-pressed":c,"--n-tab-color":u,"--n-tab-font-weight":p,"--n-tab-font-weight-active":h,"--n-tab-padding":D,"--n-tab-padding-vertical":O,"--n-tab-gap":ee,"--n-tab-gap-vertical":te,"--n-pane-padding-left":l(E,`left`),"--n-pane-padding-right":l(E,`right`),"--n-pane-padding-top":l(E,`top`),"--n-pane-padding-bottom":l(E,`bottom`),"--n-font-weight-strong":v,"--n-tab-color-segment":b}}),xe=r?h(`tabs`,D(()=>`${y.value[0]}${e.type[0]}`),Z,e):void 0;return Object.assign({mergedClsPrefix:n,mergedValue:S,renderedNames:new Set,segmentCapsuleElRef:G,tabsPaneWrapperRef:M,tabsElRef:o,barElRef:s,addTabInstRef:u,xScrollInstRef:d,scrollWrapperElRef:c,addTabFixed:J,tabWrapperStyle:T,handleNavResize:he,mergedSize:y,handleScroll:ye,handleTabsResize:_e,cssVars:r?void 0:Z,themeClass:xe?.themeClass,animationDirection:B,renderNameListRef:ie,yScrollElRef:f,handleSegmentResize:X,onAnimationBeforeLeave:F,onAnimationEnter:I,onAnimationAfterEnter:R,onRender:xe?.onRender},be)},render(){let{mergedClsPrefix:e,type:t,placement:n,addTabFixed:r,addable:i,mergedSize:a,renderNameListRef:o,onRender:s,paneWrapperClass:c,paneWrapperStyle:l,$slots:{default:u,prefix:d,suffix:f}}=this;s?.();let p=u?le(u()).filter(e=>e.type.__TAB_PANE__===!0):[],m=u?le(u()).filter(e=>e.type.__TAB__===!0):[],h=!m.length,g=t===`card`,_=t===`segment`,v=!g&&!_&&this.justifyContent;o.value=[];let y=()=>{let t=V(`div`,{style:this.tabWrapperStyle,class:`${e}-tabs-wrapper`},v?null:V(`div`,{class:`${e}-tabs-scroll-padding`,style:n===`top`||n===`bottom`?{width:`${this.tabsPadding}px`}:{height:`${this.tabsPadding}px`}}),h?p.map((e,t)=>(o.value.push(e.props.name),rt(V(Xe,Object.assign({},e.props,{internalCreatedByPane:!0,internalLeftPadded:t!==0&&(!v||v===`center`||v===`start`||v===`end`)}),e.children?{default:e.children.tab}:void 0)))):m.map((e,t)=>(o.value.push(e.props.name),rt(t!==0&&!v?nt(e):e))),!r&&i&&g?tt(i,(h?p.length:m.length)!==0):null,v?null:V(`div`,{class:`${e}-tabs-scroll-padding`,style:{width:`${this.tabsPadding}px`}}));return V(`div`,{ref:`tabsElRef`,class:`${e}-tabs-nav-scroll-content`},g&&i?V(W,{onResize:this.handleTabsResize},{default:()=>t}):t,g?V(`div`,{class:`${e}-tabs-pad`}):null,g?null:V(`div`,{ref:`barElRef`,class:`${e}-tabs-bar`}))},b=_?`top`:n;return V(`div`,{class:[`${e}-tabs`,this.themeClass,`${e}-tabs--${t}-type`,`${e}-tabs--${a}-size`,v&&`${e}-tabs--flex`,`${e}-tabs--${b}`],style:this.cssVars},V(`div`,{class:[`${e}-tabs-nav--${t}-type`,`${e}-tabs-nav--${b}`,`${e}-tabs-nav`]},K(d,t=>t&&V(`div`,{class:`${e}-tabs-nav__prefix`},t)),_?V(W,{onResize:this.handleSegmentResize},{default:()=>V(`div`,{class:`${e}-tabs-rail`,ref:`tabsElRef`},V(`div`,{class:`${e}-tabs-capsule`,ref:`segmentCapsuleElRef`},V(`div`,{class:`${e}-tabs-wrapper`},V(`div`,{class:`${e}-tabs-tab`}))),h?p.map((e,t)=>(o.value.push(e.props.name),V(Xe,Object.assign({},e.props,{internalCreatedByPane:!0,internalLeftPadded:t!==0}),e.children?{default:e.children.tab}:void 0))):m.map((e,t)=>(o.value.push(e.props.name),t===0?e:nt(e))))}):V(W,{onResize:this.handleNavResize},{default:()=>V(`div`,{class:`${e}-tabs-nav-scroll-wrapper`,ref:`scrollWrapperElRef`},[`top`,`bottom`].includes(b)?V(De,{ref:`xScrollInstRef`,onScroll:this.handleScroll},{default:y}):V(`div`,{class:`${e}-tabs-nav-y-scroll`,onScroll:this.handleScroll,ref:`yScrollElRef`},y()))}),r&&i&&g?tt(i,!0):null,K(f,t=>t&&V(`div`,{class:`${e}-tabs-nav__suffix`},t))),h&&(this.animated&&(b===`top`||b===`bottom`)?V(`div`,{ref:`tabsPaneWrapperRef`,style:l,class:[`${e}-tabs-pane-wrapper`,c]},et(p,this.mergedValue,this.renderedNames,this.onAnimationBeforeLeave,this.onAnimationEnter,this.onAnimationAfterEnter,this.animationDirection)):et(p,this.mergedValue,this.renderedNames)))}});function et(e,t,n,r,i,a,o){let s=[];return e.forEach(e=>{let{name:r,displayDirective:i,"display-directive":a}=e.props,o=e=>i===e||a===e,c=t===r;if(e.key!==void 0&&(e.key=r),c||o(`show`)||o(`show:lazy`)&&n.has(r)){n.has(r)||n.add(r);let t=!o(`if`);s.push(t?ue(e,[[T,c]]):e)}}),o?V(p,{name:`${o}-transition`,onBeforeLeave:r,onEnter:i,onAfterEnter:a},{default:()=>s}):s}function tt(e,t){return V(Xe,{ref:`addTabInstRef`,key:`__addable`,name:`__addable`,internalCreatedByPane:!0,internalAddable:!0,internalLeftPadded:t,disabled:typeof e==`object`&&e.disabled})}function nt(e){let t=E(e);return t.props?t.props.internalLeftPadded=!0:t.props={internalLeftPadded:!0},t}function rt(e){return Array.isArray(e.dynamicProps)?e.dynamicProps.includes(`internalLeftPadded`)||e.dynamicProps.push(`internalLeftPadded`):e.dynamicProps=[`internalLeftPadded`],e}var it={style:{margin:`0`}},at={style:{"font-size":`24px`,"font-weight":`600`}},ot={style:{color:`#999`,"font-size":`12px`}},st=I({__name:`StockDetailPage`,setup(l){let d=we(),f=D(()=>d.params.code||`600519`),p=z(!1),m=z({}),h=z({}),g=z({candles:[]}),_=z({trend:[]}),v=z({field_audit:[],batch_audit:[]}),b=z(`raw`),S=z(250),C=z(),w=z(`annual`),T=z(5),E=[{label:`1年`,value:1},{label:`3年`,value:3},{label:`5年`,value:5},{label:`10年`,value:10},{label:`全部`,value:99}];async function O(){p.value=!0;try{await Promise.all([ee(),k(),A(),j(),N()])}finally{p.value=!1}}async function ee(){try{let e=await a.get(`/api/stock/${f.value}/info`);m.value=e.data}catch{m.value={}}}async function k(){try{let e=await a.get(`/api/stock/${f.value}/indicators`);h.value=e.data}catch{h.value={}}}async function A(){try{let e=await a.get(`/api/stock/${f.value}/kline`,{params:{adjust:b.value,days:S.value}});g.value=e.data,ne()}catch{g.value={candles:[]}}}async function j(){try{let e=await a.get(`/api/stock/${f.value}/financial-trend`,{params:{period:w.value,years:T.value}});_.value=e.data}catch{_.value={trend:[]}}}async function N(){try{let e=await a.get(`/api/stock/${f.value}/source-audit`);v.value=e.data}catch{v.value={field_audit:[],batch_audit:[]}}}function ne(){!C.value||!g.value.candles?.length||Te(()=>import(`./index.esm-typ-WjS5.js`).then(e=>{let t=e.init(C.value);if(!t)return;let n=g.value.candles.map(e=>({timestamp:new Date(e.trade_date).getTime(),open:e.open,high:e.high,low:e.low,close:e.close,volume:e.volume,turnover:e.turnover}));t.applyNewData&&t.applyNewData(n),t.createIndicator(`MA`)}),[])}function F(e,t=2){return e==null?`—`:typeof e==`number`?Math.abs(e)>=1e8?(e/1e8).toFixed(t)+`亿`:Math.abs(e)>=1e4?(e/1e4).toFixed(t)+`万`:e.toFixed(t):e}function I(e){return e==null?`—`:(e*100).toFixed(2)+`%`}function L(e){return e==null?null:typeof e==`object`&&`value`in e?e.value:e}function R(e){return typeof e==`object`&&`historical_capable`in e&&e.historical_capable===!1}return q(b,A),q(S,A),q(w,j),q(T,j),q(f,O),oe(O),(a,l)=>{let d=te(`n-data-table`);return P(),G(`div`,null,[H(y(Se),{show:p.value},{default:c(()=>[H(y(e),{size:`small`,style:{"margin-bottom":`16px`}},{default:c(()=>[H(y(X),{align:`center`,justify:`space-between`},{default:c(()=>[H(y(X),{align:`center`},{default:c(()=>[x(`h2`,it,u(m.value.name||f.value),1),m.value.exchange?(P(),s(y(n),{key:0,size:`small`},{default:c(()=>[B(u(m.value.exchange),1)]),_:1})):M(``,!0),m.value.is_st?(P(),s(y(n),{key:1,size:`small`,type:`warning`},{default:c(()=>[...l[4]||=[B(`ST`,-1)]]),_:1})):M(``,!0),m.value.is_suspended?(P(),s(y(n),{key:2,size:`small`,type:`error`},{default:c(()=>[...l[5]||=[B(`停牌`,-1)]]),_:1})):M(``,!0)]),_:1}),H(y(X),{align:`center`},{default:c(()=>[x(`span`,at,u(F(m.value.latest_close)),1),x(`span`,ot,u(m.value.latest_price_date),1)]),_:1})]),_:1}),H(y(xe),{column:4,size:`small`,style:{"margin-top":`8px`}},{default:c(()=>[H(y(Z),{label:`代码`},{default:c(()=>[B(u(m.value.stock_code),1)]),_:1}),H(y(Z),{label:`拼音`},{default:c(()=>[B(u(m.value.pinyin),1)]),_:1}),H(y(Z),{label:`上市日期`},{default:c(()=>[B(u(m.value.listing_date),1)]),_:1}),H(y(Z),{label:`申万一级`},{default:c(()=>[B(u(m.value.sw_level1||`—`),1)]),_:1})]),_:1})]),_:1}),H(y(e),{title:`K线图`,size:`small`,style:{"margin-bottom":`16px`}},{"header-extra":c(()=>[H(y(X),null,{default:c(()=>[H(y(Y),{value:b.value,"onUpdate:value":l[0]||=e=>b.value=e,size:`small`},{default:c(()=>[H(y(Q),{value:`raw`},{default:c(()=>[...l[6]||=[B(`不复权`,-1)]]),_:1}),H(y(Q),{value:`qfq`},{default:c(()=>[...l[7]||=[B(`前复权`,-1)]]),_:1})]),_:1},8,[`value`]),H(y(ye),{value:S.value,"onUpdate:value":l[1]||=e=>S.value=e,options:[{label:`250日`,value:250},{label:`500日`,value:500},{label:`1000日`,value:1e3}],size:`small`,style:{width:`100px`}},null,8,[`value`])]),_:1})]),default:c(()=>[x(`div`,{ref_key:`klineRef`,ref:C,style:{height:`400px`,width:`100%`}},null,512),g.value.candles?.length?M(``,!0):(P(),s(y(o),{key:0,description:`无K线数据`,style:{padding:`40px`}}))]),_:1}),H(y($e),{type:`line`,style:{"margin-bottom":`16px`}},{default:c(()=>[H(y($),{name:`valuation`,tab:`估值`},{default:c(()=>[H(y(i),{cols:4,"x-gap":12,"y-gap":12},{default:c(()=>[H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`PE-TTM`,value:F(L(h.value.indicators?.valuation?.pe_ttm))},null,8,[`value`]),R(h.value.indicators?.valuation?.pe_ttm)?(P(),s(y(n),{key:0,size:`tiny`,type:`warning`,style:{"margin-top":`4px`}},{default:c(()=>[...l[8]||=[B(`仅当前`,-1)]]),_:1})):M(``,!0)]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`PB-MRQ`,value:F(L(h.value.indicators?.valuation?.pb_mrq))},null,8,[`value`]),R(h.value.indicators?.valuation?.pb_mrq)?(P(),s(y(n),{key:0,size:`tiny`,type:`warning`,style:{"margin-top":`4px`}},{default:c(()=>[...l[9]||=[B(`仅当前`,-1)]]),_:1})):M(``,!0)]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`PS-TTM`,value:F(L(h.value.indicators?.valuation?.ps_ttm))},null,8,[`value`]),R(h.value.indicators?.valuation?.ps_ttm)?(P(),s(y(n),{key:0,size:`tiny`,type:`warning`,style:{"margin-top":`4px`}},{default:c(()=>[...l[10]||=[B(`仅当前`,-1)]]),_:1})):M(``,!0)]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`PCF-TTM`,value:F(L(h.value.indicators?.valuation?.pcf_ttm))},null,8,[`value`]),R(h.value.indicators?.valuation?.pcf_ttm)?(P(),s(y(n),{key:0,size:`tiny`,type:`warning`,style:{"margin-top":`4px`}},{default:c(()=>[...l[11]||=[B(`仅当前`,-1)]]),_:1})):M(``,!0)]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`股息率`,value:I(L(h.value.indicators?.valuation?.dividend_yield))},null,8,[`value`]),R(h.value.indicators?.valuation?.dividend_yield)?(P(),s(y(n),{key:0,size:`tiny`,type:`warning`,style:{"margin-top":`4px`}},{default:c(()=>[...l[12]||=[B(`仅当前`,-1)]]),_:1})):M(``,!0)]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`总市值`,value:F(L(h.value.indicators?.valuation?.total_market_cap),0)},null,8,[`value`]),R(h.value.indicators?.valuation?.total_market_cap)?(P(),s(y(n),{key:0,size:`tiny`,type:`warning`,style:{"margin-top":`4px`}},{default:c(()=>[...l[13]||=[B(`仅当前`,-1)]]),_:1})):M(``,!0)]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`流通市值`,value:F(L(h.value.indicators?.valuation?.circ_market_cap),0)},null,8,[`value`]),R(h.value.indicators?.valuation?.circ_market_cap)?(P(),s(y(n),{key:0,size:`tiny`,type:`warning`,style:{"margin-top":`4px`}},{default:c(()=>[...l[14]||=[B(`仅当前`,-1)]]),_:1})):M(``,!0)]),_:1})]),_:1})]),_:1})]),_:1}),H(y($),{name:`profitability`,tab:`盈利`},{default:c(()=>[H(y(i),{cols:4,"x-gap":12,"y-gap":12},{default:c(()=>[H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`ROE`,value:I(L(h.value.indicators?.profitability?.roe))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`ROA`,value:I(L(h.value.indicators?.profitability?.roa))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`毛利率`,value:I(L(h.value.indicators?.profitability?.gross_margin))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`净利率`,value:I(L(h.value.indicators?.profitability?.net_margin))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`ROIC`,value:I(L(h.value.indicators?.profitability?.roic))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`CF/净利润`,value:F(L(h.value.indicators?.profitability?.cf_to_net_profit))},null,8,[`value`])]),_:1})]),_:1})]),_:1})]),_:1}),H(y($),{name:`growth`,tab:`成长`},{default:c(()=>[H(y(i),{cols:4,"x-gap":12,"y-gap":12},{default:c(()=>[H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`营收YoY`,value:I(L(h.value.indicators?.growth?.revenue_yoy))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`净利YoY`,value:I(L(h.value.indicators?.growth?.net_profit_yoy))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`扣非YoY`,value:I(L(h.value.indicators?.growth?.deducted_profit_yoy))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`营收CAGR3`,value:I(L(h.value.indicators?.growth?.revenue_cagr3))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`营收CAGR5`,value:I(L(h.value.indicators?.growth?.revenue_cagr5))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`净利CAGR5`,value:I(L(h.value.indicators?.growth?.net_profit_cagr5))},null,8,[`value`])]),_:1})]),_:1})]),_:1})]),_:1}),H(y($),{name:`safety`,tab:`安全`},{default:c(()=>[H(y(i),{cols:4,"x-gap":12,"y-gap":12},{default:c(()=>[H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`资产负债率`,value:I(L(h.value.indicators?.safety?.debt_ratio))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`流动比率`,value:F(L(h.value.indicators?.safety?.current_ratio))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`速动比率`,value:F(L(h.value.indicators?.safety?.quick_ratio))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`有息负债`,value:F(L(h.value.indicators?.safety?.interest_bearing_debt),0)},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`利息保障倍数`,value:F(L(h.value.indicators?.safety?.interest_coverage))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`商誉占比`,value:I(L(h.value.indicators?.safety?.goodwill_ratio))},null,8,[`value`])]),_:1})]),_:1})]),_:1})]),_:1}),H(y($),{name:`return`,tab:`股东回报`},{default:c(()=>[H(y(i),{cols:4,"x-gap":12,"y-gap":12},{default:c(()=>[H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`分红率`,value:I(L(h.value.indicators?.shareholder_return?.payout_ratio))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`每股股息`,value:F(L(h.value.indicators?.shareholder_return?.dps))},null,8,[`value`])]),_:1})]),_:1}),H(y(t),null,{default:c(()=>[H(y(e),{size:`small`},{default:c(()=>[H(y(r),{label:`连续分红年数`,value:L(h.value.indicators?.shareholder_return?.consecutive_div_years)??`—`},null,8,[`value`])]),_:1})]),_:1})]),_:1})]),_:1}),H(y($),{name:`custom`,tab:`自定义指标`},{default:c(()=>[H(y(X),{vertical:``},{default:c(()=>[H(y(X),null,{default:c(()=>[...l[15]||=[x(`span`,{style:{color:`#999`,"font-size":`12px`}},`选择字段查看趋势（逗号分隔）:`,-1)]]),_:1}),H(d,{size:`small`,striped:``,columns:[{title:`报告期`,key:`report_date`,width:110},{title:`营收`,key:`revenue`,render:e=>F(e.revenue,0)},{title:`归母净利`,key:`parent_net_profit`,render:e=>F(e.parent_net_profit,0)},{title:`毛利率`,key:`gross_margin`,render:e=>I(e.gross_margin)},{title:`ROE`,key:`roe`,render:e=>I(e.roe)}],data:_.value.trend,pagination:{pageSize:20},"scroll-x":800},null,8,[`columns`,`data`])]),_:1})]),_:1})]),_:1}),H(y(e),{title:`财务趋势`,size:`small`,style:{"margin-bottom":`16px`}},{"header-extra":c(()=>[H(y(X),null,{default:c(()=>[H(y(Y),{value:w.value,"onUpdate:value":l[2]||=e=>w.value=e,size:`small`},{default:c(()=>[H(y(Q),{value:`annual`},{default:c(()=>[...l[16]||=[B(`年度`,-1)]]),_:1}),H(y(Q),{value:`quarterly`},{default:c(()=>[...l[17]||=[B(`季度`,-1)]]),_:1}),H(y(Q),{value:`ttm`},{default:c(()=>[...l[18]||=[B(`TTM`,-1)]]),_:1})]),_:1},8,[`value`]),H(y(ye),{value:T.value,"onUpdate:value":l[3]||=e=>T.value=e,options:E,size:`small`,style:{width:`80px`}},null,8,[`value`])]),_:1})]),default:c(()=>[_.value.trend?.length?(P(),s(d,{key:1,size:`small`,striped:``,columns:[{title:`报告期`,key:`report_date`,width:110},{title:`营收`,key:`revenue`,render:e=>F(e.revenue,0)},{title:`归母净利`,key:`net_profit`,render:e=>F(e.net_profit,0)},{title:`扣非净利`,key:`deducted_net_profit`,render:e=>F(e.deducted_net_profit,0)},{title:`毛利率`,key:`gross_margin`,render:e=>I(e.gross_margin)},{title:`净利率`,key:`net_margin`,render:e=>I(e.net_margin)},{title:`ROE`,key:`roe`,render:e=>I(e.roe)},{title:`负债率`,key:`debt_ratio`,render:e=>I(e.debt_ratio)},{title:`EPS`,key:`basic_eps`,render:e=>F(e.basic_eps)},{title:`经营CF`,key:`cf_from_operating`,render:e=>F(e.cf_from_operating,0)}],data:_.value.trend,pagination:{pageSize:20},"scroll-x":1e3},null,8,[`columns`,`data`])):(P(),s(y(o),{key:0,description:`无财务趋势数据`,style:{padding:`40px`}}))]),_:1}),H(y(e),{title:`数据溯源`,size:`small`},{"header-extra":c(()=>[H(y(_e),{size:`small`,tag:`a`,href:`/api/stock/${f.value}/pdf-list`,target:`_blank`},{default:c(()=>[...l[19]||=[B(`PDF列表`,-1)]]),_:1},8,[`href`])]),default:c(()=>[!v.value.field_audit?.length&&!v.value.batch_audit?.length?(P(),s(y(o),{key:0,description:`无溯源数据`,style:{padding:`20px`}})):(P(),G(ce,{key:1},[l[20]||=x(`h4`,{style:{margin:`0 0 8px`}},`关键字段溯源`,-1),v.value.field_audit?.length?(P(),s(d,{key:0,size:`small`,striped:``,columns:[{title:`字段`,key:`field_name`,width:150},{title:`报告期`,key:`report_date`,width:110},{title:`值`,key:`value`,render:e=>F(e.value,4)},{title:`来源`,key:`source`,width:120},{title:`置信度`,key:`confidence`,width:80,render:e=>e.confidence===`strict`?`strict`:`approx`},{title:`抓取时间`,key:`fetch_time`,width:160}],data:v.value.field_audit,pagination:{pageSize:10}},null,8,[`columns`,`data`])):M(``,!0),l[21]||=x(`h4`,{style:{margin:`16px 0 8px`}},`批次溯源`,-1),v.value.batch_audit?.length?(P(),s(d,{key:1,size:`small`,striped:``,columns:[{title:`数据类型`,key:`data_type`,width:150},{title:`来源`,key:`source`,width:120},{title:`行数`,key:`row_count`,width:80},{title:`置信度`,key:`confidence`,width:80},{title:`抓取时间`,key:`fetch_time`,width:160}],data:v.value.batch_audit,pagination:{pageSize:10}},null,8,[`data`])):M(``,!0)],64))]),_:1})]),_:1},8,[`show`])])}}});export{st as default};