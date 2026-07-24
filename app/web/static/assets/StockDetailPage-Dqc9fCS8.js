import{a as e,i as t,l as n,n as r,p as i,r as a,t as o,u as s}from"./axios-BGo-glI3.js";import{$ as c,B as l,C as u,H as d,I as f,J as p,M as m,N as h,Q as g,U as _,V as v,_ as y,b,c as x,d as S,f as C,g as w,h as T,i as E,k as ee,l as D,mt as O,p as k,u as A,w as j,x as M}from"./runtime-core.esm-bundler-C-_igBqR.js";import{At as N,Bt as P,Ft as F,G as I,Ht as L,It as te,K as ne,Lt as R,Mt as z,Nt as B,Pt as V,R as H,St as U,V as re,at as ie,bt as W,c as ae,dt as G,f as K,ft as oe,i as q,it as J,kt as Y,lt as se,m as ce,q as le,rt as ue,st as X,tt as de,wt as fe}from"./Scrollbar-CnuoQI0d.js";import{F as pe,M as me,N as he,l as ge,u as _e,v as ve,y as ye}from"./Popover-DaAcamKQ.js";import{i as be,m as xe,n as Se,o as Ce,r as we,t as Z}from"./Space-Bnx8WTH5.js";import{t as Te}from"./Spin-j3tv6xqU.js";import{m as Ee,t as De}from"./index-BqDen4ty.js";var Oe=ve(`.v-x-scroll`,{overflow:`auto`,scrollbarWidth:`none`},[ve(`&::-webkit-scrollbar`,{width:0,height:0})]),ke=y({name:`XScroll`,props:{disabled:Boolean,onScroll:Function},setup(){let e=p(null);function t(e){!(e.currentTarget.offsetWidth<e.currentTarget.scrollWidth)||e.deltaY===0||(e.currentTarget.scrollLeft+=e.deltaY+e.deltaX,e.preventDefault())}let n=se();return Oe.mount({id:`vueuc/x-scroll`,head:!0,anchorMetaName:ye,ssr:n}),Object.assign({selfRef:e,handleWheel:t},{scrollTo(...t){var n;(n=e.value)==null||n.scrollTo(...t)}})},render(){return b(`div`,{ref:`selfRef`,onScroll:this.onScroll,onWheel:this.disabled?void 0:this.handleWheel,class:`v-x-scroll`},this.$slots)}});function Ae(e,t=`default`,n=[]){let{children:r}=e;if(typeof r==`object`&&r&&!Array.isArray(r)){let e=r[t];if(typeof e==`function`)return e()}return n}var je=/\s/;function Me(e){for(var t=e.length;t--&&je.test(e.charAt(t)););return t}var Ne=/^\s+/;function Pe(e){return e&&e.slice(0,Me(e)+1).replace(Ne,``)}var Fe=NaN,Ie=/^[-+]0x[0-9a-f]+$/i,Le=/^0b[01]+$/i,Re=/^0o[0-7]+$/i,ze=parseInt;function Be(e){if(typeof e==`number`)return e;if(re(e))return Fe;if(H(e)){var t=typeof e.valueOf==`function`?e.valueOf():e;e=H(t)?t+``:t}if(typeof e!=`string`)return e===0?e:+e;e=Pe(e);var n=Le.test(e);return n||Re.test(e)?ze(e.slice(2),n?2:8):Ie.test(e)?Fe:+e}var Ve=function(){return I.Date.now()},He=`Expected a function`,Ue=Math.max,We=Math.min;function Ge(e,t,n){var r,i,a,o,s,c,l=0,u=!1,d=!1,f=!0;if(typeof e!=`function`)throw TypeError(He);t=Be(t)||0,H(n)&&(u=!!n.leading,d=`maxWait`in n,a=d?Ue(Be(n.maxWait)||0,t):a,f=`trailing`in n?!!n.trailing:f);function p(t){var n=r,a=i;return r=i=void 0,l=t,o=e.apply(a,n),o}function m(e){return l=e,s=setTimeout(_,t),u?p(e):o}function h(e){var n=e-c,r=e-l,i=t-n;return d?We(i,a-r):i}function g(e){var n=e-c,r=e-l;return c===void 0||n>=t||n<0||d&&r>=a}function _(){var e=Ve();if(g(e))return v(e);s=setTimeout(_,h(e))}function v(e){return s=void 0,f&&r?p(e):(r=i=void 0,o)}function y(){s!==void 0&&clearTimeout(s),l=0,r=c=i=s=void 0}function b(){return s===void 0?o:v(Ve())}function x(){var e=Ve(),n=g(e);if(r=arguments,i=this,c=e,n){if(s===void 0)return m(c);if(d)return clearTimeout(s),s=setTimeout(_,t),p(c)}return s===void 0&&(s=setTimeout(_,t)),o}return x.cancel=y,x.flush=b,x}var Ke=`Expected a function`;function qe(e,t,n){var r=!0,i=!0;if(typeof e!=`function`)throw TypeError(Ke);return H(n)&&(r=`leading`in n?!!n.leading:r,i=`trailing`in n?!!n.trailing:i),Ge(e,t,{leading:r,maxWait:t,trailing:i})}var Q=y({name:`RadioButton`,props:we,setup:be,render(){let{mergedClsPrefix:e}=this;return b(`label`,{class:[`${e}-radio-button`,this.mergedDisabled&&`${e}-radio-button--disabled`,this.renderSafeChecked&&`${e}-radio-button--checked`,this.focus&&[`${e}-radio-button--focus`]]},b(`input`,{ref:`inputRef`,type:`radio`,class:`${e}-radio-input`,value:this.value,name:this.mergedName,checked:this.renderSafeChecked,disabled:this.mergedDisabled,onChange:this.handleRadioInputChange,onFocus:this.handleRadioInputFocus,onBlur:this.handleRadioInputBlur}),b(`div`,{class:`${e}-radio-button__state-border`}),de(this.$slots.default,t=>!t&&!this.label?null:b(`div`,{ref:`labelRef`,class:`${e}-radio__label`},t||this.label)))}}),Je={thPaddingBorderedSmall:`8px 12px`,thPaddingBorderedMedium:`12px 16px`,thPaddingBorderedLarge:`16px 24px`,thPaddingSmall:`0`,thPaddingMedium:`0`,thPaddingLarge:`0`,tdPaddingBorderedSmall:`8px 12px`,tdPaddingBorderedMedium:`12px 16px`,tdPaddingBorderedLarge:`16px 24px`,tdPaddingSmall:`0 0 8px 0`,tdPaddingMedium:`0 0 12px 0`,tdPaddingLarge:`0 0 16px 0`};function Ye(e){let{tableHeaderColor:t,textColor2:n,textColor1:r,cardColor:i,modalColor:a,popoverColor:o,dividerColor:s,borderRadius:c,fontWeightStrong:l,lineHeight:u,fontSizeSmall:d,fontSizeMedium:f,fontSizeLarge:p}=e;return Object.assign(Object.assign({},Je),{lineHeight:u,fontSizeSmall:d,fontSizeMedium:f,fontSizeLarge:p,titleTextColor:r,thColor:W(i,t),thColorModal:W(a,t),thColorPopover:W(o,t),thTextColor:r,thFontWeight:l,tdTextColor:n,tdColor:i,tdColorModal:a,tdColorPopover:o,borderColor:W(i,s),borderColorModal:W(a,s),borderColorPopover:W(o,s),borderRadius:c})}var Xe={name:`Descriptions`,common:q,self:Ye},Ze=Y([N(`descriptions`,{fontSize:`var(--n-font-size)`},[N(`descriptions-separator`,`
 display: inline-block;
 margin: 0 8px 0 2px;
 `),N(`descriptions-table-wrapper`,[N(`descriptions-table`,[N(`descriptions-table-row`,[N(`descriptions-table-header`,{padding:`var(--n-th-padding)`}),N(`descriptions-table-content`,{padding:`var(--n-td-padding)`})])])]),V(`bordered`,[N(`descriptions-table-wrapper`,[N(`descriptions-table`,[N(`descriptions-table-row`,[Y(`&:last-child`,[N(`descriptions-table-content`,{paddingBottom:0})])])])])]),B(`left-label-placement`,[N(`descriptions-table-content`,[Y(`> *`,{verticalAlign:`top`})])]),B(`left-label-align`,[Y(`th`,{textAlign:`left`})]),B(`center-label-align`,[Y(`th`,{textAlign:`center`})]),B(`right-label-align`,[Y(`th`,{textAlign:`right`})]),B(`bordered`,[N(`descriptions-table-wrapper`,`
 border-radius: var(--n-border-radius);
 overflow: hidden;
 background: var(--n-merged-td-color);
 border: 1px solid var(--n-merged-border-color);
 `,[N(`descriptions-table`,[N(`descriptions-table-row`,[Y(`&:not(:last-child)`,[N(`descriptions-table-content`,{borderBottom:`1px solid var(--n-merged-border-color)`}),N(`descriptions-table-header`,{borderBottom:`1px solid var(--n-merged-border-color)`})]),N(`descriptions-table-header`,`
 font-weight: 400;
 background-clip: padding-box;
 background-color: var(--n-merged-th-color);
 `,[Y(`&:not(:last-child)`,{borderRight:`1px solid var(--n-merged-border-color)`})]),N(`descriptions-table-content`,[Y(`&:not(:last-child)`,{borderRight:`1px solid var(--n-merged-border-color)`})])])])])]),N(`descriptions-header`,`
 font-weight: var(--n-th-font-weight);
 font-size: 18px;
 transition: color .3s var(--n-bezier);
 line-height: var(--n-line-height);
 margin-bottom: 16px;
 color: var(--n-title-text-color);
 `),N(`descriptions-table-wrapper`,`
 transition:
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier);
 `,[N(`descriptions-table`,`
 width: 100%;
 border-collapse: separate;
 border-spacing: 0;
 box-sizing: border-box;
 `,[N(`descriptions-table-row`,`
 box-sizing: border-box;
 transition: border-color .3s var(--n-bezier);
 `,[N(`descriptions-table-header`,`
 font-weight: var(--n-th-font-weight);
 line-height: var(--n-line-height);
 display: table-cell;
 box-sizing: border-box;
 color: var(--n-th-text-color);
 transition:
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier);
 `),N(`descriptions-table-content`,`
 vertical-align: top;
 line-height: var(--n-line-height);
 display: table-cell;
 box-sizing: border-box;
 color: var(--n-td-text-color);
 transition:
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier);
 `,[z(`content`,`
 transition: color .3s var(--n-bezier);
 display: inline-block;
 color: var(--n-td-text-color);
 `)]),z(`label`,`
 font-weight: var(--n-th-font-weight);
 transition: color .3s var(--n-bezier);
 display: inline-block;
 margin-right: 14px;
 color: var(--n-th-text-color);
 `)])])])]),N(`descriptions-table-wrapper`,`
 --n-merged-th-color: var(--n-th-color);
 --n-merged-td-color: var(--n-td-color);
 --n-merged-border-color: var(--n-border-color);
 `),te(N(`descriptions-table-wrapper`,`
 --n-merged-th-color: var(--n-th-color-modal);
 --n-merged-td-color: var(--n-td-color-modal);
 --n-merged-border-color: var(--n-border-color-modal);
 `)),R(N(`descriptions-table-wrapper`,`
 --n-merged-th-color: var(--n-th-color-popover);
 --n-merged-td-color: var(--n-td-color-popover);
 --n-merged-border-color: var(--n-border-color-popover);
 `))]),Qe=`DESCRIPTION_ITEM_FLAG`;function $e(e){return typeof e==`object`&&e&&!Array.isArray(e)?e.type&&e.type.DESCRIPTION_ITEM_FLAG:!1}var et=y({name:`Descriptions`,props:Object.assign(Object.assign({},ce.props),{title:String,column:{type:Number,default:3},columns:Number,labelPlacement:{type:String,default:`top`},labelAlign:{type:String,default:`left`},separator:{type:String,default:`:`},size:String,bordered:Boolean,labelClass:String,labelStyle:[Object,String],contentClass:String,contentStyle:[Object,String]}),slots:Object,setup(e){let{mergedClsPrefixRef:t,inlineThemeDisabled:n,mergedComponentPropsRef:r}=le(e),i=D(()=>e.size||r?.value?.Descriptions?.size||`medium`),a=ce(`Descriptions`,`-descriptions`,Ze,Xe,e,t),o=D(()=>{let{bordered:t}=e,n=i.value,{common:{cubicBezierEaseInOut:r},self:{titleTextColor:o,thColor:s,thColorModal:c,thColorPopover:l,thTextColor:u,thFontWeight:d,tdTextColor:f,tdColor:p,tdColorModal:m,tdColorPopover:h,borderColor:g,borderColorModal:_,borderColorPopover:v,borderRadius:y,lineHeight:b,[F(`fontSize`,n)]:x,[F(t?`thPaddingBordered`:`thPadding`,n)]:S,[F(t?`tdPaddingBordered`:`tdPadding`,n)]:C}}=a.value;return{"--n-title-text-color":o,"--n-th-padding":S,"--n-td-padding":C,"--n-font-size":x,"--n-bezier":r,"--n-th-font-weight":d,"--n-line-height":b,"--n-th-text-color":u,"--n-td-text-color":f,"--n-th-color":s,"--n-th-color-modal":c,"--n-th-color-popover":l,"--n-td-color":p,"--n-td-color-modal":m,"--n-td-color-popover":h,"--n-border-radius":y,"--n-border-color":g,"--n-border-color-modal":_,"--n-border-color-popover":v}}),s=n?ne(`descriptions`,D(()=>{let t=``,{bordered:n}=e;return n&&(t+=`a`),t+=i.value[0],t}),o,e):void 0;return{mergedClsPrefix:t,cssVars:n?void 0:o,themeClass:s?.themeClass,onRender:s?.onRender,compitableColumn:oe(e,[`columns`,`column`]),inlineThemeDisabled:n,mergedSize:i}},render(){let e=this.$slots.default,t=e?ue(e()):[];t.length;let{contentClass:n,labelClass:r,compitableColumn:a,labelPlacement:o,labelAlign:s,mergedSize:c,bordered:l,title:u,cssVars:d,mergedClsPrefix:f,separator:p,onRender:m}=this;m?.();let h=t.filter(e=>$e(e)),g=h.reduce((e,t,i)=>{let s=t.props||{},c=h.length-1===i,u=[`label`in s?s.label:Ae(t,`label`)],d=[Ae(t)],m=s.span||1,g=e.span;e.span+=m;let _=s.labelStyle||s[`label-style`]||this.labelStyle,v=s.contentStyle||s[`content-style`]||this.contentStyle;if(o===`left`)l?e.row.push(b(`th`,{class:[`${f}-descriptions-table-header`,r],colspan:1,style:_},u),b(`td`,{class:[`${f}-descriptions-table-content`,n],colspan:c?(a-g)*2+1:m*2-1,style:v},d)):e.row.push(b(`td`,{class:`${f}-descriptions-table-content`,colspan:c?(a-g)*2:m*2},b(`span`,{class:[`${f}-descriptions-table-content__label`,r],style:_},[...u,p&&b(`span`,{class:`${f}-descriptions-separator`},p)]),b(`span`,{class:[`${f}-descriptions-table-content__content`,n],style:v},d)));else{let t=c?(a-g)*2:m*2;e.row.push(b(`th`,{class:[`${f}-descriptions-table-header`,r],colspan:t,style:_},u)),e.secondRow.push(b(`td`,{class:[`${f}-descriptions-table-content`,n],colspan:t,style:v},d))}return(e.span>=a||c)&&(e.span=0,e.row.length&&(e.rows.push(e.row),e.row=[]),o!==`left`&&e.secondRow.length&&(e.rows.push(e.secondRow),e.secondRow=[])),e},{span:0,row:[],secondRow:[],rows:[]}).rows.map(e=>b(`tr`,{class:`${f}-descriptions-table-row`},e));return b(`div`,{style:d,class:[`${f}-descriptions`,this.themeClass,`${f}-descriptions--${o}-label-placement`,`${f}-descriptions--${s}-label-align`,`${f}-descriptions--${c}-size`,l&&`${f}-descriptions--bordered`]},u||this.$slots.header?b(`div`,{class:`${f}-descriptions-header`},u||i(this,`header`)):null,b(`div`,{class:`${f}-descriptions-table-wrapper`},b(`table`,{class:`${f}-descriptions-table`},b(`tbody`,null,o===`top`&&b(`tr`,{class:`${f}-descriptions-table-row`,style:{visibility:`collapse`}},pe(a*2,b(`td`,null))),g))))}}),tt={label:String,span:{type:Number,default:1},labelClass:String,labelStyle:[Object,String],contentClass:String,contentStyle:[Object,String]},nt=y({name:`DescriptionsItem`,[Qe]:!0,props:tt,slots:Object,render(){return null}}),rt={tabFontSizeSmall:`14px`,tabFontSizeMedium:`14px`,tabFontSizeLarge:`16px`,tabGapSmallLine:`36px`,tabGapMediumLine:`36px`,tabGapLargeLine:`36px`,tabGapSmallLineVertical:`8px`,tabGapMediumLineVertical:`8px`,tabGapLargeLineVertical:`8px`,tabPaddingSmallLine:`6px 0`,tabPaddingMediumLine:`10px 0`,tabPaddingLargeLine:`14px 0`,tabPaddingVerticalSmallLine:`6px 12px`,tabPaddingVerticalMediumLine:`8px 16px`,tabPaddingVerticalLargeLine:`10px 20px`,tabGapSmallBar:`36px`,tabGapMediumBar:`36px`,tabGapLargeBar:`36px`,tabGapSmallBarVertical:`8px`,tabGapMediumBarVertical:`8px`,tabGapLargeBarVertical:`8px`,tabPaddingSmallBar:`4px 0`,tabPaddingMediumBar:`6px 0`,tabPaddingLargeBar:`10px 0`,tabPaddingVerticalSmallBar:`6px 12px`,tabPaddingVerticalMediumBar:`8px 16px`,tabPaddingVerticalLargeBar:`10px 20px`,tabGapSmallCard:`4px`,tabGapMediumCard:`4px`,tabGapLargeCard:`4px`,tabGapSmallCardVertical:`4px`,tabGapMediumCardVertical:`4px`,tabGapLargeCardVertical:`4px`,tabPaddingSmallCard:`8px 16px`,tabPaddingMediumCard:`10px 20px`,tabPaddingLargeCard:`12px 24px`,tabPaddingSmallSegment:`4px 0`,tabPaddingMediumSegment:`6px 0`,tabPaddingLargeSegment:`8px 0`,tabPaddingVerticalLargeSegment:`0 8px`,tabPaddingVerticalSmallCard:`8px 12px`,tabPaddingVerticalMediumCard:`10px 16px`,tabPaddingVerticalLargeCard:`12px 20px`,tabPaddingVerticalSmallSegment:`0 4px`,tabPaddingVerticalMediumSegment:`0 6px`,tabGapSmallSegment:`0`,tabGapMediumSegment:`0`,tabGapLargeSegment:`0`,tabGapSmallSegmentVertical:`0`,tabGapMediumSegmentVertical:`0`,tabGapLargeSegmentVertical:`0`,panePaddingSmall:`8px 0 0 0`,panePaddingMedium:`12px 0 0 0`,panePaddingLarge:`16px 0 0 0`,closeSize:`18px`,closeIconSize:`14px`};function it(e){let{textColor2:t,primaryColor:n,textColorDisabled:r,closeIconColor:i,closeIconColorHover:a,closeIconColorPressed:o,closeColorHover:s,closeColorPressed:c,tabColor:l,baseColor:u,dividerColor:d,fontWeight:f,textColor1:p,borderRadius:m,fontSize:h,fontWeightStrong:g}=e;return Object.assign(Object.assign({},rt),{colorSegment:l,tabFontSizeCard:h,tabTextColorLine:p,tabTextColorActiveLine:n,tabTextColorHoverLine:n,tabTextColorDisabledLine:r,tabTextColorSegment:p,tabTextColorActiveSegment:t,tabTextColorHoverSegment:t,tabTextColorDisabledSegment:r,tabTextColorBar:p,tabTextColorActiveBar:n,tabTextColorHoverBar:n,tabTextColorDisabledBar:r,tabTextColorCard:p,tabTextColorHoverCard:p,tabTextColorActiveCard:n,tabTextColorDisabledCard:r,barColor:n,closeIconColor:i,closeIconColorHover:a,closeIconColorPressed:o,closeColorHover:s,closeColorPressed:c,closeBorderRadius:m,tabColor:l,tabColorSegment:u,tabBorderColor:d,tabFontWeightActive:f,tabFontWeight:f,tabBorderRadius:m,paneTextColor:t,fontWeightStrong:g})}var at={name:`Tabs`,common:q,self:it},ot=G(`n-tabs`),st={tab:[String,Number,Object,Function],name:{type:[String,Number],required:!0},disabled:Boolean,displayDirective:{type:String,default:`if`},closable:{type:Boolean,default:void 0},tabProps:Object,label:[String,Number,Object,Function]},$=y({__TAB_PANE__:!0,name:`TabPane`,alias:[`TabPanel`],props:st,slots:Object,setup(e){let t=M(ot,null);return t||ie(`tab-pane`,"`n-tab-pane` must be placed inside `n-tabs`."),{style:t.paneStyleRef,class:t.paneClassRef,mergedClsPrefix:t.mergedClsPrefixRef}},render(){return b(`div`,{class:[`${this.mergedClsPrefix}-tab-pane`,this.class],style:this.style},this.$slots)}}),ct=y({__TAB__:!0,inheritAttrs:!1,name:`Tab`,props:Object.assign({internalLeftPadded:Boolean,internalAddable:Boolean,internalCreatedByPane:Boolean},_e(st,[`displayDirective`])),setup(e){let{mergedClsPrefixRef:t,valueRef:n,typeRef:r,closableRef:i,tabStyleRef:a,addTabStyleRef:o,tabClassRef:s,addTabClassRef:c,tabChangeIdRef:l,onBeforeLeaveRef:u,triggerRef:d,handleAdd:f,activateTab:p,handleClose:m}=M(ot);return{trigger:d,mergedClosable:D(()=>{if(e.internalAddable)return!1;let{closable:t}=e;return t===void 0?i.value:t}),style:a,addStyle:o,tabClass:s,addTabClass:c,clsPrefix:t,value:n,type:r,handleClose(t){t.stopPropagation(),!e.disabled&&m(e.name)},activateTab(){if(e.disabled)return;if(e.internalAddable){f();return}let{name:t}=e,r=++l.id;if(t!==n.value){let{value:i}=u;i?Promise.resolve(i(e.name,n.value)).then(e=>{e&&l.id===r&&p(t)}):p(t)}}}},render(){let{internalAddable:e,clsPrefix:t,name:n,disabled:r,label:i,tab:a,value:o,mergedClosable:s,trigger:c,$slots:{default:l}}=this,d=i??a;return b(`div`,{class:`${t}-tabs-tab-wrapper`},this.internalLeftPadded?b(`div`,{class:`${t}-tabs-tab-pad`}):null,b(`div`,Object.assign({key:n,"data-name":n,"data-disabled":r?!0:void 0},u({class:[`${t}-tabs-tab`,o===n&&`${t}-tabs-tab--active`,r&&`${t}-tabs-tab--disabled`,s&&`${t}-tabs-tab--closable`,e&&`${t}-tabs-tab--addable`,e?this.addTabClass:this.tabClass],onClick:c===`click`?this.activateTab:void 0,onMouseenter:c===`hover`?this.activateTab:void 0,style:e?this.addStyle:this.style},this.internalCreatedByPane?this.tabProps||{}:this.$attrs)),b(`span`,{class:`${t}-tabs-tab__label`},e?b(E,null,b(`div`,{class:`${t}-tabs-tab__height-placeholder`},`\xA0`),b(K,{clsPrefix:t},{default:()=>b(xe,null)})):l?l():typeof d==`object`?d:ge(d??n)),s&&this.type===`card`?b(ae,{clsPrefix:t,class:`${t}-tabs-tab__close`,onClick:this.handleClose,disabled:r}):null))}}),lt=N(`tabs`,`
 box-sizing: border-box;
 width: 100%;
 display: flex;
 flex-direction: column;
 transition:
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier);
`,[B(`segment-type`,[N(`tabs-rail`,[Y(`&.transition-disabled`,[N(`tabs-capsule`,`
 transition: none;
 `)])])]),B(`top`,[N(`tab-pane`,`
 padding: var(--n-pane-padding-top) var(--n-pane-padding-right) var(--n-pane-padding-bottom) var(--n-pane-padding-left);
 `)]),B(`left`,[N(`tab-pane`,`
 padding: var(--n-pane-padding-right) var(--n-pane-padding-bottom) var(--n-pane-padding-left) var(--n-pane-padding-top);
 `)]),B(`left, right`,`
 flex-direction: row;
 `,[N(`tabs-bar`,`
 width: 2px;
 right: 0;
 transition:
 top .2s var(--n-bezier),
 max-height .2s var(--n-bezier),
 background-color .3s var(--n-bezier);
 `),N(`tabs-tab`,`
 padding: var(--n-tab-padding-vertical); 
 `)]),B(`right`,`
 flex-direction: row-reverse;
 `,[N(`tab-pane`,`
 padding: var(--n-pane-padding-left) var(--n-pane-padding-top) var(--n-pane-padding-right) var(--n-pane-padding-bottom);
 `),N(`tabs-bar`,`
 left: 0;
 `)]),B(`bottom`,`
 flex-direction: column-reverse;
 justify-content: flex-end;
 `,[N(`tab-pane`,`
 padding: var(--n-pane-padding-bottom) var(--n-pane-padding-right) var(--n-pane-padding-top) var(--n-pane-padding-left);
 `),N(`tabs-bar`,`
 top: 0;
 `)]),N(`tabs-rail`,`
 position: relative;
 padding: 3px;
 border-radius: var(--n-tab-border-radius);
 width: 100%;
 background-color: var(--n-color-segment);
 transition: background-color .3s var(--n-bezier);
 display: flex;
 align-items: center;
 `,[N(`tabs-capsule`,`
 border-radius: var(--n-tab-border-radius);
 position: absolute;
 pointer-events: none;
 background-color: var(--n-tab-color-segment);
 box-shadow: 0 1px 3px 0 rgba(0, 0, 0, .08);
 transition: transform 0.3s var(--n-bezier);
 `),N(`tabs-tab-wrapper`,`
 flex-basis: 0;
 flex-grow: 1;
 display: flex;
 align-items: center;
 justify-content: center;
 `,[N(`tabs-tab`,`
 overflow: hidden;
 border-radius: var(--n-tab-border-radius);
 width: 100%;
 display: flex;
 align-items: center;
 justify-content: center;
 `,[B(`active`,`
 font-weight: var(--n-font-weight-strong);
 color: var(--n-tab-text-color-active);
 `),Y(`&:hover`,`
 color: var(--n-tab-text-color-hover);
 `)])])]),B(`flex`,[N(`tabs-nav`,`
 width: 100%;
 position: relative;
 `,[N(`tabs-wrapper`,`
 width: 100%;
 `,[N(`tabs-tab`,`
 margin-right: 0;
 `)])])]),N(`tabs-nav`,`
 box-sizing: border-box;
 line-height: 1.5;
 display: flex;
 transition: border-color .3s var(--n-bezier);
 `,[z(`prefix, suffix`,`
 display: flex;
 align-items: center;
 `),z(`prefix`,`padding-right: 16px;`),z(`suffix`,`padding-left: 16px;`)]),B(`top, bottom`,[Y(`>`,[N(`tabs-nav`,[N(`tabs-nav-scroll-wrapper`,[Y(`&::before`,`
 top: 0;
 bottom: 0;
 left: 0;
 width: 20px;
 `),Y(`&::after`,`
 top: 0;
 bottom: 0;
 right: 0;
 width: 20px;
 `),B(`shadow-start`,[Y(`&::before`,`
 box-shadow: inset 10px 0 8px -8px rgba(0, 0, 0, .12);
 `)]),B(`shadow-end`,[Y(`&::after`,`
 box-shadow: inset -10px 0 8px -8px rgba(0, 0, 0, .12);
 `)])])])])]),B(`left, right`,[N(`tabs-nav-scroll-content`,`
 flex-direction: column;
 `),Y(`>`,[N(`tabs-nav`,[N(`tabs-nav-scroll-wrapper`,[Y(`&::before`,`
 top: 0;
 left: 0;
 right: 0;
 height: 20px;
 `),Y(`&::after`,`
 bottom: 0;
 left: 0;
 right: 0;
 height: 20px;
 `),B(`shadow-start`,[Y(`&::before`,`
 box-shadow: inset 0 10px 8px -8px rgba(0, 0, 0, .12);
 `)]),B(`shadow-end`,[Y(`&::after`,`
 box-shadow: inset 0 -10px 8px -8px rgba(0, 0, 0, .12);
 `)])])])])]),N(`tabs-nav-scroll-wrapper`,`
 flex: 1;
 position: relative;
 overflow: hidden;
 `,[N(`tabs-nav-y-scroll`,`
 height: 100%;
 width: 100%;
 overflow-y: auto; 
 scrollbar-width: none;
 `,[Y(`&::-webkit-scrollbar, &::-webkit-scrollbar-track-piece, &::-webkit-scrollbar-thumb`,`
 width: 0;
 height: 0;
 display: none;
 `)]),Y(`&::before, &::after`,`
 transition: box-shadow .3s var(--n-bezier);
 pointer-events: none;
 content: "";
 position: absolute;
 z-index: 1;
 `)]),N(`tabs-nav-scroll-content`,`
 display: flex;
 position: relative;
 min-width: 100%;
 min-height: 100%;
 width: fit-content;
 box-sizing: border-box;
 `),N(`tabs-wrapper`,`
 display: inline-flex;
 flex-wrap: nowrap;
 position: relative;
 `),N(`tabs-tab-wrapper`,`
 display: flex;
 flex-wrap: nowrap;
 flex-shrink: 0;
 flex-grow: 0;
 `),N(`tabs-tab`,`
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
 `,[B(`disabled`,{cursor:`not-allowed`}),z(`close`,`
 margin-left: 6px;
 transition:
 background-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 `),z(`label`,`
 display: flex;
 align-items: center;
 z-index: 1;
 `)]),N(`tabs-bar`,`
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
 `,[Y(`&.transition-disabled`,`
 transition: none;
 `),B(`disabled`,`
 background-color: var(--n-tab-text-color-disabled)
 `)]),N(`tabs-pane-wrapper`,`
 position: relative;
 overflow: hidden;
 transition: max-height .2s var(--n-bezier);
 `),N(`tab-pane`,`
 color: var(--n-pane-text-color);
 width: 100%;
 transition:
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 opacity .2s var(--n-bezier);
 left: 0;
 right: 0;
 top: 0;
 `,[Y(`&.next-transition-leave-active, &.prev-transition-leave-active, &.next-transition-enter-active, &.prev-transition-enter-active`,`
 transition:
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 transform .2s var(--n-bezier),
 opacity .2s var(--n-bezier);
 `),Y(`&.next-transition-leave-active, &.prev-transition-leave-active`,`
 position: absolute;
 `),Y(`&.next-transition-enter-from, &.prev-transition-leave-to`,`
 transform: translateX(32px);
 opacity: 0;
 `),Y(`&.next-transition-leave-to, &.prev-transition-enter-from`,`
 transform: translateX(-32px);
 opacity: 0;
 `),Y(`&.next-transition-leave-from, &.next-transition-enter-to, &.prev-transition-leave-from, &.prev-transition-enter-to`,`
 transform: translateX(0);
 opacity: 1;
 `)]),N(`tabs-tab-pad`,`
 box-sizing: border-box;
 width: var(--n-tab-gap);
 flex-grow: 0;
 flex-shrink: 0;
 `),B(`line-type, bar-type`,[N(`tabs-tab`,`
 font-weight: var(--n-tab-font-weight);
 box-sizing: border-box;
 vertical-align: bottom;
 `,[Y(`&:hover`,{color:`var(--n-tab-text-color-hover)`}),B(`active`,`
 color: var(--n-tab-text-color-active);
 font-weight: var(--n-tab-font-weight-active);
 `),B(`disabled`,{color:`var(--n-tab-text-color-disabled)`})])]),N(`tabs-nav`,[B(`line-type`,[B(`top`,[z(`prefix, suffix`,`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),N(`tabs-nav-scroll-content`,`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),N(`tabs-bar`,`
 bottom: -1px;
 `)]),B(`left`,[z(`prefix, suffix`,`
 border-right: 1px solid var(--n-tab-border-color);
 `),N(`tabs-nav-scroll-content`,`
 border-right: 1px solid var(--n-tab-border-color);
 `),N(`tabs-bar`,`
 right: -1px;
 `)]),B(`right`,[z(`prefix, suffix`,`
 border-left: 1px solid var(--n-tab-border-color);
 `),N(`tabs-nav-scroll-content`,`
 border-left: 1px solid var(--n-tab-border-color);
 `),N(`tabs-bar`,`
 left: -1px;
 `)]),B(`bottom`,[z(`prefix, suffix`,`
 border-top: 1px solid var(--n-tab-border-color);
 `),N(`tabs-nav-scroll-content`,`
 border-top: 1px solid var(--n-tab-border-color);
 `),N(`tabs-bar`,`
 top: -1px;
 `)]),z(`prefix, suffix`,`
 transition: border-color .3s var(--n-bezier);
 `),N(`tabs-nav-scroll-content`,`
 transition: border-color .3s var(--n-bezier);
 `),N(`tabs-bar`,`
 border-radius: 0;
 `)]),B(`card-type`,[z(`prefix, suffix`,`
 transition: border-color .3s var(--n-bezier);
 `),N(`tabs-pad`,`
 flex-grow: 1;
 transition: border-color .3s var(--n-bezier);
 `),N(`tabs-tab-pad`,`
 transition: border-color .3s var(--n-bezier);
 `),N(`tabs-tab`,`
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
 `,[B(`addable`,`
 padding-left: 8px;
 padding-right: 8px;
 font-size: 16px;
 justify-content: center;
 `,[z(`height-placeholder`,`
 width: 0;
 font-size: var(--n-tab-font-size);
 `),V(`disabled`,[Y(`&:hover`,`
 color: var(--n-tab-text-color-hover);
 `)])]),B(`closable`,`padding-right: 8px;`),B(`active`,`
 background-color: #0000;
 font-weight: var(--n-tab-font-weight-active);
 color: var(--n-tab-text-color-active);
 `),B(`disabled`,`color: var(--n-tab-text-color-disabled);`)])]),B(`left, right`,`
 flex-direction: column; 
 `,[z(`prefix, suffix`,`
 padding: var(--n-tab-padding-vertical);
 `),N(`tabs-wrapper`,`
 flex-direction: column;
 `),N(`tabs-tab-wrapper`,`
 flex-direction: column;
 `,[N(`tabs-tab-pad`,`
 height: var(--n-tab-gap-vertical);
 width: 100%;
 `)])]),B(`top`,[B(`card-type`,[N(`tabs-scroll-padding`,`border-bottom: 1px solid var(--n-tab-border-color);`),z(`prefix, suffix`,`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),N(`tabs-tab`,`
 border-top-left-radius: var(--n-tab-border-radius);
 border-top-right-radius: var(--n-tab-border-radius);
 `,[B(`active`,`
 border-bottom: 1px solid #0000;
 `)]),N(`tabs-tab-pad`,`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),N(`tabs-pad`,`
 border-bottom: 1px solid var(--n-tab-border-color);
 `)])]),B(`left`,[B(`card-type`,[N(`tabs-scroll-padding`,`border-right: 1px solid var(--n-tab-border-color);`),z(`prefix, suffix`,`
 border-right: 1px solid var(--n-tab-border-color);
 `),N(`tabs-tab`,`
 border-top-left-radius: var(--n-tab-border-radius);
 border-bottom-left-radius: var(--n-tab-border-radius);
 `,[B(`active`,`
 border-right: 1px solid #0000;
 `)]),N(`tabs-tab-pad`,`
 border-right: 1px solid var(--n-tab-border-color);
 `),N(`tabs-pad`,`
 border-right: 1px solid var(--n-tab-border-color);
 `)])]),B(`right`,[B(`card-type`,[N(`tabs-scroll-padding`,`border-left: 1px solid var(--n-tab-border-color);`),z(`prefix, suffix`,`
 border-left: 1px solid var(--n-tab-border-color);
 `),N(`tabs-tab`,`
 border-top-right-radius: var(--n-tab-border-radius);
 border-bottom-right-radius: var(--n-tab-border-radius);
 `,[B(`active`,`
 border-left: 1px solid #0000;
 `)]),N(`tabs-tab-pad`,`
 border-left: 1px solid var(--n-tab-border-color);
 `),N(`tabs-pad`,`
 border-left: 1px solid var(--n-tab-border-color);
 `)])]),B(`bottom`,[B(`card-type`,[N(`tabs-scroll-padding`,`border-top: 1px solid var(--n-tab-border-color);`),z(`prefix, suffix`,`
 border-top: 1px solid var(--n-tab-border-color);
 `),N(`tabs-tab`,`
 border-bottom-left-radius: var(--n-tab-border-radius);
 border-bottom-right-radius: var(--n-tab-border-radius);
 `,[B(`active`,`
 border-top: 1px solid #0000;
 `)]),N(`tabs-tab-pad`,`
 border-top: 1px solid var(--n-tab-border-color);
 `),N(`tabs-pad`,`
 border-top: 1px solid var(--n-tab-border-color);
 `)])])])]),ut=qe,dt=y({name:`Tabs`,props:Object.assign(Object.assign({},ce.props),{value:[String,Number],defaultValue:[String,Number],trigger:{type:String,default:`click`},type:{type:String,default:`bar`},closable:Boolean,justifyContent:String,size:String,placement:{type:String,default:`top`},tabStyle:[String,Object],tabClass:String,addTabStyle:[String,Object],addTabClass:String,barWidth:Number,paneClass:String,paneStyle:[String,Object],paneWrapperClass:String,paneWrapperStyle:[String,Object],addable:[Boolean,Object],tabsPadding:{type:Number,default:0},animated:Boolean,onBeforeLeave:Function,onAdd:Function,"onUpdate:value":[Function,Array],onUpdateValue:[Function,Array],onClose:[Function,Array],labelSize:String,activeName:[String,Number],onActiveNameChange:[Function,Array]}),slots:Object,setup(e,{slots:t}){let{mergedClsPrefixRef:n,inlineThemeDisabled:r,mergedComponentPropsRef:i}=le(e),a=ce(`Tabs`,`-tabs`,lt,at,e,n),o=p(null),s=p(null),c=p(null),u=p(null),d=p(null),f=p(null),m=p(!0),_=p(!0),y=oe(e,[`labelSize`,`size`]),b=D(()=>y.value?y.value:i?.value?.Tabs?.size||`medium`),x=oe(e,[`activeName`,`value`]),S=p(x.value??e.defaultValue??(t.default?ue(t.default())[0]?.props?.name:null)),C=me(x,S),w={id:0},T=D(()=>{if(!(!e.justifyContent||e.type===`card`))return{display:`flex`,justifyContent:e.justifyContent}});l(C,()=>{w.id=0,M(),N()});function E(){let{value:e}=C;return e===null?null:o.value?.querySelector(`[data-name="${e}"]`)}function O(t){if(e.type===`card`)return;let{value:r}=s;if(!r)return;let i=r.style.opacity===`0`;if(t){let a=`${n.value}-tabs-bar--disabled`,{barWidth:o,placement:s}=e;if(t.dataset.disabled===`true`?r.classList.add(a):r.classList.remove(a),[`top`,`bottom`].includes(s)){if(A([`top`,`maxHeight`,`height`]),typeof o==`number`&&t.offsetWidth>=o){let e=Math.floor((t.offsetWidth-o)/2)+t.offsetLeft;r.style.left=`${e}px`,r.style.maxWidth=`${o}px`}else r.style.left=`${t.offsetLeft}px`,r.style.maxWidth=`${t.offsetWidth}px`;r.style.width=`8192px`,i&&(r.style.transition=`none`),r.offsetWidth,i&&(r.style.transition=``,r.style.opacity=`1`)}else{if(A([`left`,`maxWidth`,`width`]),typeof o==`number`&&t.offsetHeight>=o){let e=Math.floor((t.offsetHeight-o)/2)+t.offsetTop;r.style.top=`${e}px`,r.style.maxHeight=`${o}px`}else r.style.top=`${t.offsetTop}px`,r.style.maxHeight=`${t.offsetHeight}px`;r.style.height=`8192px`,i&&(r.style.transition=`none`),r.offsetHeight,i&&(r.style.transition=``,r.style.opacity=`1`)}}}function k(){if(e.type===`card`)return;let{value:t}=s;t&&(t.style.opacity=`0`)}function A(e){let{value:t}=s;if(t)for(let n of e)t.style[n]=``}function M(){if(e.type===`card`)return;let t=E();t?O(t):k()}function N(){let e=d.value?.$el;if(!e)return;let t=E();if(!t)return;let{scrollLeft:n,offsetWidth:r}=e,{offsetLeft:i,offsetWidth:a}=t;n>i?e.scrollTo({top:0,left:i,behavior:`smooth`}):i+a>n+r&&e.scrollTo({top:0,left:i+a-r,behavior:`smooth`})}let P=p(null),I=0,L=null;function te(e){let t=P.value;if(t){I=e.getBoundingClientRect().height;let n=`${I}px`,r=()=>{t.style.height=n,t.style.maxHeight=n};L?(r(),L(),L=null):L=r}}function R(e){let t=P.value;if(t){let n=e.getBoundingClientRect().height,r=()=>{document.body.offsetHeight,t.style.maxHeight=`${n}px`,t.style.height=`${Math.max(I,n)}px`};L?(L(),L=null,r()):L=r}}function z(){let t=P.value;if(t){t.style.maxHeight=``,t.style.height=``;let{paneWrapperStyle:n}=e;if(typeof n==`string`)t.style.cssText=n;else if(n){let{maxHeight:e,height:r}=n;e!==void 0&&(t.style.maxHeight=e),r!==void 0&&(t.style.height=r)}}}let B={value:[]},V=p(`next`);function H(e){let t=C.value,n=`next`;for(let r of B.value){if(r===t)break;if(r===e){n=`prev`;break}}V.value=n,re(e)}function re(t){let{onActiveNameChange:n,onUpdateValue:r,"onUpdate:value":i}=e;n&&J(n,t),r&&J(r,t),i&&J(i,t),S.value=t}function ie(t){let{onClose:n}=e;n&&J(n,t)}let W=!0;function ae(){let{value:e}=s;if(!e)return;W||=!1;let t=`transition-disabled`;e.classList.add(t),M(),e.classList.remove(t)}let G=p(null);function K({transitionDisabled:e}){let t=o.value;if(!t)return;e&&t.classList.add(`transition-disabled`);let n=E();n&&G.value&&(G.value.style.width=`${n.offsetWidth}px`,G.value.style.height=`${n.offsetHeight}px`,G.value.style.transform=`translateX(${n.offsetLeft-U(getComputedStyle(t).paddingLeft)}px)`,e&&G.value.offsetWidth),e&&t.classList.remove(`transition-disabled`)}l([C],()=>{e.type===`segment`&&j(()=>{K({transitionDisabled:!1})})}),ee(()=>{e.type===`segment`&&K({transitionDisabled:!0})});let q=0;function Y(t){if(t.contentRect.width===0&&t.contentRect.height===0||q===t.contentRect.width)return;q=t.contentRect.width;let{type:n}=e;if((n===`line`||n===`bar`)&&(W||e.justifyContent?.startsWith(`space`))&&ae(),n!==`segment`){let{placement:t}=e;_e((t===`top`||t===`bottom`?d.value?.$el:f.value)||null)}}let se=ut(Y,64);l([()=>e.justifyContent,()=>e.size],()=>{j(()=>{let{type:t}=e;(t===`line`||t===`bar`)&&ae()})});let X=p(!1);function de(t){let{target:n,contentRect:{width:r,height:i}}=t,a=n.parentElement.parentElement.offsetWidth,o=n.parentElement.parentElement.offsetHeight,{placement:s}=e;if(!X.value)s===`top`||s===`bottom`?a<r&&(X.value=!0):o<i&&(X.value=!0);else{let{value:e}=u;if(!e)return;s===`top`||s===`bottom`?a-r>e.$el.offsetWidth&&(X.value=!1):o-i>e.$el.offsetHeight&&(X.value=!1)}_e(d.value?.$el||null)}let pe=ut(de,64);function ge(){let{onAdd:t}=e;t&&t(),j(()=>{let e=E(),{value:t}=d;!e||!t||t.scrollTo({left:e.offsetLeft,top:0,behavior:`smooth`})})}function _e(t){if(!t)return;let{placement:n}=e;if(n===`top`||n===`bottom`){let{scrollLeft:e,scrollWidth:n,offsetWidth:r}=t;m.value=e<=0,_.value=e+r>=n}else{let{scrollTop:e,scrollHeight:n,offsetHeight:r}=t;m.value=e<=0,_.value=e+r>=n}}let ve=ut(e=>{_e(e.target)},64);h(ot,{triggerRef:g(e,`trigger`),tabStyleRef:g(e,`tabStyle`),tabClassRef:g(e,`tabClass`),addTabStyleRef:g(e,`addTabStyle`),addTabClassRef:g(e,`addTabClass`),paneClassRef:g(e,`paneClass`),paneStyleRef:g(e,`paneStyle`),mergedClsPrefixRef:n,typeRef:g(e,`type`),closableRef:g(e,`closable`),valueRef:C,tabChangeIdRef:w,onBeforeLeaveRef:g(e,`onBeforeLeave`),activateTab:H,handleClose:ie,handleAdd:ge}),he(()=>{M(),N()}),v(()=>{let{value:e}=c;if(!e)return;let{value:t}=n,r=`${t}-tabs-nav-scroll-wrapper--shadow-start`,i=`${t}-tabs-nav-scroll-wrapper--shadow-end`;m.value?e.classList.remove(r):e.classList.add(r),_.value?e.classList.remove(i):e.classList.add(i)});let ye={syncBarPosition:()=>{M()}},be=()=>{K({transitionDisabled:!0})},xe=D(()=>{let{value:t}=b,{type:n}=e,r=`${t}${{card:`Card`,bar:`Bar`,line:`Line`,segment:`Segment`}[n]}`,{self:{barColor:i,closeIconColor:o,closeIconColorHover:s,closeIconColorPressed:c,tabColor:l,tabBorderColor:u,paneTextColor:d,tabFontWeight:f,tabBorderRadius:p,tabFontWeightActive:m,colorSegment:h,fontWeightStrong:g,tabColorSegment:_,closeSize:v,closeIconSize:y,closeColorHover:x,closeColorPressed:S,closeBorderRadius:C,[F(`panePadding`,t)]:w,[F(`tabPadding`,r)]:T,[F(`tabPaddingVertical`,r)]:E,[F(`tabGap`,r)]:ee,[F(`tabGap`,`${r}Vertical`)]:D,[F(`tabTextColor`,n)]:O,[F(`tabTextColorActive`,n)]:k,[F(`tabTextColorHover`,n)]:A,[F(`tabTextColorDisabled`,n)]:j,[F(`tabFontSize`,t)]:M},common:{cubicBezierEaseInOut:N}}=a.value;return{"--n-bezier":N,"--n-color-segment":h,"--n-bar-color":i,"--n-tab-font-size":M,"--n-tab-text-color":O,"--n-tab-text-color-active":k,"--n-tab-text-color-disabled":j,"--n-tab-text-color-hover":A,"--n-pane-text-color":d,"--n-tab-border-color":u,"--n-tab-border-radius":p,"--n-close-size":v,"--n-close-icon-size":y,"--n-close-color-hover":x,"--n-close-color-pressed":S,"--n-close-border-radius":C,"--n-close-icon-color":o,"--n-close-icon-color-hover":s,"--n-close-icon-color-pressed":c,"--n-tab-color":l,"--n-tab-font-weight":f,"--n-tab-font-weight-active":m,"--n-tab-padding":T,"--n-tab-padding-vertical":E,"--n-tab-gap":ee,"--n-tab-gap-vertical":D,"--n-pane-padding-left":fe(w,`left`),"--n-pane-padding-right":fe(w,`right`),"--n-pane-padding-top":fe(w,`top`),"--n-pane-padding-bottom":fe(w,`bottom`),"--n-font-weight-strong":g,"--n-tab-color-segment":_}}),Se=r?ne(`tabs`,D(()=>`${b.value[0]}${e.type[0]}`),xe,e):void 0;return Object.assign({mergedClsPrefix:n,mergedValue:C,renderedNames:new Set,segmentCapsuleElRef:G,tabsPaneWrapperRef:P,tabsElRef:o,barElRef:s,addTabInstRef:u,xScrollInstRef:d,scrollWrapperElRef:c,addTabFixed:X,tabWrapperStyle:T,handleNavResize:se,mergedSize:b,handleScroll:ve,handleTabsResize:pe,cssVars:r?void 0:xe,themeClass:Se?.themeClass,animationDirection:V,renderNameListRef:B,yScrollElRef:f,handleSegmentResize:be,onAnimationBeforeLeave:te,onAnimationEnter:R,onAnimationAfterEnter:z,onRender:Se?.onRender},ye)},render(){let{mergedClsPrefix:e,type:t,placement:n,addTabFixed:r,addable:i,mergedSize:a,renderNameListRef:o,onRender:s,paneWrapperClass:c,paneWrapperStyle:l,$slots:{default:u,prefix:d,suffix:f}}=this;s?.();let p=u?ue(u()).filter(e=>e.type.__TAB_PANE__===!0):[],m=u?ue(u()).filter(e=>e.type.__TAB__===!0):[],h=!m.length,g=t===`card`,_=t===`segment`,v=!g&&!_&&this.justifyContent;o.value=[];let y=()=>{let t=b(`div`,{style:this.tabWrapperStyle,class:`${e}-tabs-wrapper`},v?null:b(`div`,{class:`${e}-tabs-scroll-padding`,style:n===`top`||n===`bottom`?{width:`${this.tabsPadding}px`}:{height:`${this.tabsPadding}px`}}),h?p.map((e,t)=>(o.value.push(e.props.name),ht(b(ct,Object.assign({},e.props,{internalCreatedByPane:!0,internalLeftPadded:t!==0&&(!v||v===`center`||v===`start`||v===`end`)}),e.children?{default:e.children.tab}:void 0)))):m.map((e,t)=>(o.value.push(e.props.name),ht(t!==0&&!v?mt(e):e))),!r&&i&&g?pt(i,(h?p.length:m.length)!==0):null,v?null:b(`div`,{class:`${e}-tabs-scroll-padding`,style:{width:`${this.tabsPadding}px`}}));return b(`div`,{ref:`tabsElRef`,class:`${e}-tabs-nav-scroll-content`},g&&i?b(X,{onResize:this.handleTabsResize},{default:()=>t}):t,g?b(`div`,{class:`${e}-tabs-pad`}):null,g?null:b(`div`,{ref:`barElRef`,class:`${e}-tabs-bar`}))},x=_?`top`:n;return b(`div`,{class:[`${e}-tabs`,this.themeClass,`${e}-tabs--${t}-type`,`${e}-tabs--${a}-size`,v&&`${e}-tabs--flex`,`${e}-tabs--${x}`],style:this.cssVars},b(`div`,{class:[`${e}-tabs-nav--${t}-type`,`${e}-tabs-nav--${x}`,`${e}-tabs-nav`]},de(d,t=>t&&b(`div`,{class:`${e}-tabs-nav__prefix`},t)),_?b(X,{onResize:this.handleSegmentResize},{default:()=>b(`div`,{class:`${e}-tabs-rail`,ref:`tabsElRef`},b(`div`,{class:`${e}-tabs-capsule`,ref:`segmentCapsuleElRef`},b(`div`,{class:`${e}-tabs-wrapper`},b(`div`,{class:`${e}-tabs-tab`}))),h?p.map((e,t)=>(o.value.push(e.props.name),b(ct,Object.assign({},e.props,{internalCreatedByPane:!0,internalLeftPadded:t!==0}),e.children?{default:e.children.tab}:void 0))):m.map((e,t)=>(o.value.push(e.props.name),t===0?e:mt(e))))}):b(X,{onResize:this.handleNavResize},{default:()=>b(`div`,{class:`${e}-tabs-nav-scroll-wrapper`,ref:`scrollWrapperElRef`},[`top`,`bottom`].includes(x)?b(ke,{ref:`xScrollInstRef`,onScroll:this.handleScroll},{default:y}):b(`div`,{class:`${e}-tabs-nav-y-scroll`,onScroll:this.handleScroll,ref:`yScrollElRef`},y()))}),r&&i&&g?pt(i,!0):null,de(f,t=>t&&b(`div`,{class:`${e}-tabs-nav__suffix`},t))),h&&(this.animated&&(x===`top`||x===`bottom`)?b(`div`,{ref:`tabsPaneWrapperRef`,style:l,class:[`${e}-tabs-pane-wrapper`,c]},ft(p,this.mergedValue,this.renderedNames,this.onAnimationBeforeLeave,this.onAnimationEnter,this.onAnimationAfterEnter,this.animationDirection)):ft(p,this.mergedValue,this.renderedNames)))}});function ft(e,t,n,r,i,a,o){let s=[];return e.forEach(e=>{let{name:r,displayDirective:i,"display-directive":a}=e.props,o=e=>i===e||a===e,c=t===r;if(e.key!==void 0&&(e.key=r),c||o(`show`)||o(`show:lazy`)&&n.has(r)){n.has(r)||n.add(r);let t=!o(`if`);s.push(t?_(e,[[L,c]]):e)}}),o?b(P,{name:`${o}-transition`,onBeforeLeave:r,onEnter:i,onAfterEnter:a},{default:()=>s}):s}function pt(e,t){return b(ct,{ref:`addTabInstRef`,key:`__addable`,name:`__addable`,internalCreatedByPane:!0,internalAddable:!0,internalLeftPadded:t,disabled:typeof e==`object`&&e.disabled})}function mt(e){let t=x(e);return t.props?t.props.internalLeftPadded=!0:t.props={internalLeftPadded:!0},t}function ht(e){return Array.isArray(e.dynamicProps)?e.dynamicProps.includes(`internalLeftPadded`)||e.dynamicProps.push(`internalLeftPadded`):e.dynamicProps=[`internalLeftPadded`],e}var gt={style:{margin:`0`}},_t={style:{"font-size":`24px`,"font-weight":`600`}},vt={style:{color:`#999`,"font-size":`12px`}},yt=y({__name:`StockDetailPage`,setup(i){let u=Ee(),h=D(()=>u.params.code||`600519`),g=p(!1),_=p({}),v=p({}),y=p({candles:[]}),b=p({trend:[]}),x=p({field_audit:[],batch_audit:[]}),j=p(`raw`),M=p(250),N=p(),P=p(`annual`),F=p(5),I=[{label:`1年`,value:1},{label:`3年`,value:3},{label:`5年`,value:5},{label:`10年`,value:10}];async function L(){g.value=!0;try{await Promise.all([te(),ne(),R(),z(),B()])}finally{g.value=!1}}async function te(){try{let e=await o.get(`/api/stock/${h.value}/info`);_.value=e.data}catch{_.value={}}}async function ne(){try{let e=await o.get(`/api/stock/${h.value}/indicators`);v.value=e.data}catch{v.value={}}}async function R(){try{let e=await o.get(`/api/stock/${h.value}/kline`,{params:{adjust:j.value,days:M.value}});y.value=e.data,V()}catch{y.value={candles:[]}}}async function z(){try{let e=await o.get(`/api/stock/${h.value}/financial-trend`,{params:{period:P.value,years:F.value}});b.value=e.data}catch{b.value={trend:[]}}}async function B(){try{let e=await o.get(`/api/stock/${h.value}/source-audit`);x.value=e.data}catch{x.value={field_audit:[],batch_audit:[]}}}function V(){!N.value||!y.value.candles?.length||De(()=>import(`./index.esm-typ-WjS5.js`).then(e=>{let t=e.init(N.value);if(!t)return;let n=y.value.candles.map(e=>({timestamp:new Date(e.trade_date).getTime(),open:e.open,high:e.high,low:e.low,close:e.close,volume:e.volume,turnover:e.turnover}));t.applyNewData&&t.applyNewData(n),t.createIndicator(`MA`)}),[])}function H(e,t=2){return e==null?`—`:typeof e==`number`?Math.abs(e)>=1e8?(e/1e8).toFixed(t)+`亿`:Math.abs(e)>=1e4?(e/1e4).toFixed(t)+`万`:e.toFixed(t):e}function U(e){return e==null?`—`:(e*100).toFixed(2)+`%`}return l(j,R),l(M,R),l(P,z),l(F,z),l(h,L),ee(L),(i,o)=>{let l=f(`n-data-table`);return m(),k(`div`,null,[w(c(Te),{show:g.value},{default:d(()=>[w(c(e),{size:`small`,style:{"margin-bottom":`16px`}},{default:d(()=>[w(c(Z),{align:`center`,justify:`space-between`},{default:d(()=>[w(c(Z),{align:`center`},{default:d(()=>[A(`h2`,gt,O(_.value.name||h.value),1),_.value.exchange?(m(),S(c(n),{key:0,size:`small`},{default:d(()=>[T(O(_.value.exchange),1)]),_:1})):C(``,!0),_.value.is_st?(m(),S(c(n),{key:1,size:`small`,type:`warning`},{default:d(()=>[...o[4]||=[T(`ST`,-1)]]),_:1})):C(``,!0),_.value.is_suspended?(m(),S(c(n),{key:2,size:`small`,type:`error`},{default:d(()=>[...o[5]||=[T(`停牌`,-1)]]),_:1})):C(``,!0)]),_:1}),w(c(Z),{align:`center`},{default:d(()=>[A(`span`,_t,O(H(_.value.latest_close)),1),A(`span`,vt,O(_.value.latest_price_date),1)]),_:1})]),_:1}),w(c(et),{column:4,size:`small`,style:{"margin-top":`8px`}},{default:d(()=>[w(c(nt),{label:`代码`},{default:d(()=>[T(O(_.value.stock_code),1)]),_:1}),w(c(nt),{label:`拼音`},{default:d(()=>[T(O(_.value.pinyin),1)]),_:1}),w(c(nt),{label:`上市日期`},{default:d(()=>[T(O(_.value.listing_date),1)]),_:1}),w(c(nt),{label:`申万一级`},{default:d(()=>[T(O(_.value.sw_level1||`—`),1)]),_:1})]),_:1})]),_:1}),w(c(e),{title:`K线图`,size:`small`,style:{"margin-bottom":`16px`}},{"header-extra":d(()=>[w(c(Z),null,{default:d(()=>[w(c(Se),{value:j.value,"onUpdate:value":o[0]||=e=>j.value=e,size:`small`},{default:d(()=>[w(c(Q),{value:`raw`},{default:d(()=>[...o[6]||=[T(`不复权`,-1)]]),_:1}),w(c(Q),{value:`qfq`},{default:d(()=>[...o[7]||=[T(`前复权`,-1)]]),_:1})]),_:1},8,[`value`]),w(c(Ce),{value:M.value,"onUpdate:value":o[1]||=e=>M.value=e,options:[{label:`250日`,value:250},{label:`500日`,value:500},{label:`1000日`,value:1e3}],size:`small`,style:{width:`100px`}},null,8,[`value`])]),_:1})]),default:d(()=>[A(`div`,{ref_key:`klineRef`,ref:N,style:{height:`400px`,width:`100%`}},null,512),y.value.candles?.length?C(``,!0):(m(),S(c(s),{key:0,description:`无K线数据`,style:{padding:`40px`}}))]),_:1}),w(c(dt),{type:`line`,style:{"margin-bottom":`16px`}},{default:d(()=>[w(c($),{name:`valuation`,tab:`估值`},{default:d(()=>[w(c(a),{cols:4,"x-gap":12,"y-gap":12},{default:d(()=>[w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`PE-TTM`,value:H(v.value.indicators?.valuation?.pe_ttm)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`PB-MRQ`,value:H(v.value.indicators?.valuation?.pb_mrq)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`PS-TTM`,value:H(v.value.indicators?.valuation?.ps_ttm)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`PCF-TTM`,value:H(v.value.indicators?.valuation?.pcf_ttm)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`股息率`,value:U(v.value.indicators?.valuation?.dividend_yield)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`总市值`,value:H(v.value.indicators?.valuation?.total_market_cap,0)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`流通市值`,value:H(v.value.indicators?.valuation?.circ_market_cap,0)},null,8,[`value`])]),_:1})]),_:1})]),_:1})]),_:1}),w(c($),{name:`profitability`,tab:`盈利`},{default:d(()=>[w(c(a),{cols:4,"x-gap":12,"y-gap":12},{default:d(()=>[w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`ROE`,value:U(v.value.indicators?.profitability?.roe)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`ROA`,value:U(v.value.indicators?.profitability?.roa)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`毛利率`,value:U(v.value.indicators?.profitability?.gross_margin)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`净利率`,value:U(v.value.indicators?.profitability?.net_margin)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`ROIC`,value:U(v.value.indicators?.profitability?.roic)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`CF/净利润`,value:H(v.value.indicators?.profitability?.cf_to_net_profit)},null,8,[`value`])]),_:1})]),_:1})]),_:1})]),_:1}),w(c($),{name:`growth`,tab:`成长`},{default:d(()=>[w(c(a),{cols:4,"x-gap":12,"y-gap":12},{default:d(()=>[w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`营收YoY`,value:U(v.value.indicators?.growth?.revenue_yoy)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`净利YoY`,value:U(v.value.indicators?.growth?.net_profit_yoy)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`扣非YoY`,value:U(v.value.indicators?.growth?.deducted_profit_yoy)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`营收CAGR3`,value:U(v.value.indicators?.growth?.revenue_cagr3)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`营收CAGR5`,value:U(v.value.indicators?.growth?.revenue_cagr5)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`净利CAGR5`,value:U(v.value.indicators?.growth?.net_profit_cagr5)},null,8,[`value`])]),_:1})]),_:1})]),_:1})]),_:1}),w(c($),{name:`safety`,tab:`安全`},{default:d(()=>[w(c(a),{cols:4,"x-gap":12,"y-gap":12},{default:d(()=>[w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`资产负债率`,value:U(v.value.indicators?.safety?.debt_ratio)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`流动比率`,value:H(v.value.indicators?.safety?.current_ratio)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`速动比率`,value:H(v.value.indicators?.safety?.quick_ratio)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`有息负债`,value:H(v.value.indicators?.safety?.interest_bearing_debt,0)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`利息保障倍数`,value:H(v.value.indicators?.safety?.interest_coverage)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`商誉占比`,value:U(v.value.indicators?.safety?.goodwill_ratio)},null,8,[`value`])]),_:1})]),_:1})]),_:1})]),_:1}),w(c($),{name:`return`,tab:`股东回报`},{default:d(()=>[w(c(a),{cols:4,"x-gap":12,"y-gap":12},{default:d(()=>[w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`分红率`,value:U(v.value.indicators?.shareholder_return?.payout_ratio)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`每股股息`,value:H(v.value.indicators?.shareholder_return?.dps)},null,8,[`value`])]),_:1})]),_:1}),w(c(t),null,{default:d(()=>[w(c(e),{size:`small`},{default:d(()=>[w(c(r),{label:`连续分红年数`,value:v.value.indicators?.shareholder_return?.consecutive_div_years??`—`},null,8,[`value`])]),_:1})]),_:1})]),_:1})]),_:1})]),_:1}),w(c(e),{title:`财务趋势`,size:`small`,style:{"margin-bottom":`16px`}},{"header-extra":d(()=>[w(c(Z),null,{default:d(()=>[w(c(Se),{value:P.value,"onUpdate:value":o[2]||=e=>P.value=e,size:`small`},{default:d(()=>[w(c(Q),{value:`annual`},{default:d(()=>[...o[8]||=[T(`年度`,-1)]]),_:1}),w(c(Q),{value:`quarterly`},{default:d(()=>[...o[9]||=[T(`季度`,-1)]]),_:1}),w(c(Q),{value:`ttm`},{default:d(()=>[...o[10]||=[T(`TTM`,-1)]]),_:1})]),_:1},8,[`value`]),w(c(Ce),{value:F.value,"onUpdate:value":o[3]||=e=>F.value=e,options:I,size:`small`,style:{width:`80px`}},null,8,[`value`])]),_:1})]),default:d(()=>[b.value.trend?.length?(m(),S(l,{key:1,size:`small`,striped:``,columns:[{title:`报告期`,key:`report_date`,width:110},{title:`营收`,key:`revenue`,render:e=>H(e.revenue,0)},{title:`归母净利`,key:`net_profit`,render:e=>H(e.net_profit,0)},{title:`扣非净利`,key:`deducted_net_profit`,render:e=>H(e.deducted_net_profit,0)},{title:`毛利率`,key:`gross_margin`,render:e=>U(e.gross_margin)},{title:`净利率`,key:`net_margin`,render:e=>U(e.net_margin)},{title:`ROE`,key:`roe`,render:e=>U(e.roe)},{title:`负债率`,key:`debt_ratio`,render:e=>U(e.debt_ratio)},{title:`EPS`,key:`basic_eps`,render:e=>H(e.basic_eps)},{title:`经营CF`,key:`cf_from_operating`,render:e=>H(e.cf_from_operating,0)}],data:b.value.trend,pagination:{pageSize:20},"scroll-x":1e3},null,8,[`columns`,`data`])):(m(),S(c(s),{key:0,description:`无财务趋势数据`,style:{padding:`40px`}}))]),_:1}),w(c(e),{title:`数据溯源`,size:`small`},{default:d(()=>[!x.value.field_audit?.length&&!x.value.batch_audit?.length?(m(),S(c(s),{key:0,description:`无溯源数据`,style:{padding:`20px`}})):(m(),k(E,{key:1},[o[11]||=A(`h4`,{style:{margin:`0 0 8px`}},`关键字段溯源`,-1),x.value.field_audit?.length?(m(),S(l,{key:0,size:`small`,striped:``,columns:[{title:`字段`,key:`field_name`,width:150},{title:`报告期`,key:`report_date`,width:110},{title:`值`,key:`value`,render:e=>H(e.value,4)},{title:`来源`,key:`source`,width:120},{title:`置信度`,key:`confidence`,width:80,render:e=>e.confidence===`strict`?`strict`:`approx`},{title:`抓取时间`,key:`fetch_time`,width:160}],data:x.value.field_audit,pagination:{pageSize:10}},null,8,[`columns`,`data`])):C(``,!0),o[12]||=A(`h4`,{style:{margin:`16px 0 8px`}},`批次溯源`,-1),x.value.batch_audit?.length?(m(),S(l,{key:1,size:`small`,striped:``,columns:[{title:`数据类型`,key:`data_type`,width:150},{title:`来源`,key:`source`,width:120},{title:`行数`,key:`row_count`,width:80},{title:`置信度`,key:`confidence`,width:80},{title:`抓取时间`,key:`fetch_time`,width:160}],data:x.value.batch_audit,pagination:{pageSize:10}},null,8,[`data`])):C(``,!0)],64))]),_:1})]),_:1},8,[`show`])])}}});export{yt as default};