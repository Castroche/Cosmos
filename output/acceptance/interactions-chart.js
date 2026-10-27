async page => {
 const checks=[];
 await page.getByRole('button',{name:'15m',exact:true}).click();
 await page.locator('.chart-loading').waitFor({state:'hidden',timeout:30000});
 checks.push({timeframe:await page.getByRole('button',{name:'15m',exact:true}).getAttribute('aria-pressed')});
 await page.getByRole('button',{name:/图表时区/}).click();
 checks.push({timezone:await page.getByRole('button',{name:/图表时区/}).getAttribute('aria-label')});
 await page.getByRole('button',{name:/指标 3/}).click();
 await page.getByRole('checkbox',{name:/相对强弱/}).check();
 await page.getByRole('button',{name:'关闭指标面板'}).click();
 await page.getByRole('button',{name:'普通坐标',exact:true}).click();
 await page.getByRole('menuitem',{name:'对数坐标',exact:true}).click();
 checks.push('图表指标与坐标模式通过');
 await page.getByRole('button',{name:'图表全屏',exact:true}).click();
 await page.getByRole('button',{name:'退出全屏',exact:true}).waitFor();
 await page.getByRole('button',{name:'退出全屏',exact:true}).click();
 await page.getByRole('button',{name:'水平线',exact:true}).click();
 await page.getByRole('img',{name:'图表绘图层'}).click({position:{x:160,y:160}});
 await page.getByRole('button',{name:'撤销绘图'}).click();
 checks.push('全屏和绘图撤销通过');
 await page.getByRole('button',{name:'策略编辑器',exact:true}).click();
 await page.getByRole('button',{name:'生成策略并运行回测',exact:true}).click();
 await page.getByRole('button',{name:'生成策略并运行回测',exact:true}).waitFor({timeout:60000});
 checks.push({editorResult:await page.locator('.strategy-editor-workspace').innerText()});
 return checks;
}
