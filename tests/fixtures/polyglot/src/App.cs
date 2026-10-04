using System;
using Acme.Store;
using static System.Math;

namespace Acme.App
{
    public class App : IDisposable
    {
        public const string Name = "app";

        public async Task<string> RenderAsync(string id)
        {
            var item = await Store.LoadAsync(id);
            return Format(item);
        }

        public void Dispose() { Store.Release(); }
    }

    public interface IRenderer { string Render(); }
}
