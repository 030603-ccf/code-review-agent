# PHP 高频陷阱

## 类型杂耍
- `==` 松散比较：`"0" == false`、`"abc" == 0`（PHP 8 前）
- `strpos()` 返回 0（找到首位）被 `if` 判为 false（应用 `!== false`）
- `null`、`""`、`"0"`、`0`、`[]` 在布尔上下文全为 false

## 安全
- SQL 注入：字符串拼接查询（应用 PDO 预处理语句）
- XSS：`echo $_GET['x']` 未经 `htmlspecialchars()` 转义
- 文件包含：`include $_GET['page']` 未白名单校验
- `unserialize()` 反序列化用户输入（对象注入攻击）

## 数组与字符串
- `array_merge` 数字键会重新编号（`+` 运算符不会）
- `foreach` 中引用变量 `&$v` 循环后未 `unset($v)`
- `explode` 对空串返回 `[""]` 而非 `[]`

## 错误处理
- `@` 错误抑制符隐藏关键错误
- 未设置自定义错误处理器，warning 直接输出到响应
- `try/catch` 不捕获 `Error`（PHP 7+ 的 TypeError 等是 Error 不是 Exception）

## 资源与并发
- 数据库连接/文件句柄未关闭（长脚本内存累积）
- `session_start()` 后长时间持有 session 锁（阻塞并发请求）
