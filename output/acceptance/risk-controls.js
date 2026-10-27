async page => {
 await page.setViewportSize({width:1600,height:1000});
 await page.goto('http://127.0.0.1:8010/?section=risk');
 await page.getByRole('button',{name:'紧急停止开仓',exact:true}).waitFor({timeout:30000});
 await page.evaluate(()=>window.confirm=()=>true);
 await page.getByRole('button',{name:'紧急停止开仓',exact:true}).click();
 await page.getByRole('button',{name:'恢复新开仓',exact:true}).waitFor({timeout:30000});
 await page.getByRole('button',{name:'恢复新开仓',exact:true}).click();
 await page.getByRole('button',{name:'紧急停止开仓',exact:true}).waitFor({timeout:30000});
 return {isolatedRiskHaltResume:'passed'};
}
