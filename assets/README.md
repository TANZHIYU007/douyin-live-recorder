# assets/sign.js

只有 `--engine native` 需要这个文件；默认的浏览器引擎不用。

抖音弹幕 WebSocket 的 `signature` 参数由页面里的混淆 JS（webmssdk）现场生成。
本项目不附带那份代码，你需要自己放一个 `sign.js`，约定很简单：

- 从 `process.argv[2]` 读一个字符串（X-MS-STUB，即签名字段拼接后的 MD5）
- 把算出来的 signature 打到 stdout
- 退出码 0

```js
// sign.js 骨架
const stub = process.argv[2];
// ... 在这里调用 webmssdk 的 frontierSign({ "X-MS-STUB": stub })
console.log(signature);
```

没有这个文件时，`danmaku_native.py` 会退回裸 MD5，抖音会用
`DEVICE_BLOCKED` 拒掉握手，程序重试 3 次后明确报错。
