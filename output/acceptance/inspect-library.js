async page => {
 return {buttons:await page.locator('.library-table button').evaluateAll(es=>es.map(e=>({text:e.textContent,title:e.title}))),hidden:await page.getByRole('checkbox',{name:'显示已归档/隐藏'}).isChecked()};
}
