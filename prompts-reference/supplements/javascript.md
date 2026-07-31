# JavaScript/TypeScript 高频陷阱

## 类型与比较
- `==` 触发隐式类型转换，应使用 `===` / `!==`
- `typeof null === "object"`，判断 null 用 `=== null`
- `NaN !== NaN`，必须用 `Number.isNaN()`

## 异步与闭包
- `var` 在循环中共享同一个变量（用 `let`）
- `async` 内忘 `await` 导致 Promise 静默丢错误
- `.then()` 链未 return 致后续收到 undefined

## 数组与对象
- `Array.sort()` 默认按字符串排序，数字需传比较函数
- 浅拷贝不复制嵌套对象
- `for...in` 遍历键（含原型链），`for...of` 遍历值

## 字符串与正则
- 字符串不可变，`str[i] = 'x'` 静默失败
- 正则带 `g` 标志有 lastIndex 状态，重复调用结果不同
- `parseInt` 自动识别进制，建议传第二个参数

## React/前端特有
- `useEffect` 依赖遗漏导致闭包捕获旧值
- 直接改 state 不触发重渲染
- key 用 index 在列表增删时导致状态错乱
