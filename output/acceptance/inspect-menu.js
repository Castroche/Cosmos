async page => {
 await page.getByRole('button',{name:'交易工作台',exact:true}).click();
 await page.getByRole('button',{name:/指标 \d/}).click();
 return await page.locator('.indicator-menu').evaluate(e=>{let a=[];for(let p=e;p;p=p.parentElement){let s=getComputedStyle(p);a.push({class:p.className,transform:s.transform,contain:s.contain,filter:s.filter,backdrop:s.backdropFilter,willChange:s.willChange,overflow:s.overflow,position:s.position,height:p.getBoundingClientRect().height})}return a});
}
